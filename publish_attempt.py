"""Publish ONE verified warm-worker attempt into an EMPTY candidate generation store (Linux, stdlib only).

    publish_verified_attempt(queue, attempts_dir, job_id, store=..., work_dir=..., ledger_dir=...,
                             expected_revision=None, status_file=None, stage_max_bytes=None)

Deliberately small and fail-closed. The store must be empty unless this very attempt was already published
there; a repeat re-stages the attempt and proves the store byte-exactly (pointer, inventory, every published
mesh against the VERIFIED per-file hashes, every block file against the freshly generated lookup), never by
"same chunks". An existing arbitrary generation is never replaced; an interrupted publication is never
recovered by guessing. The queue, the attempt and its verified inputs are only read. The result is an AREA
generation (one finite plan), not the full world. See PUBLISH-ATTEMPT.md.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import area_records
import capture_batch as cb
import owned_process as op
import render_status
import verify_rendered_plan as vrp
import warm_worker as ww
from batch_block_info import BatchBlockInfoError, attach_batch_block_info
import publish_generation as pg
from publish_generation import canonical, publish_generation
from render_queue import RenderQueue

LEDGER_FORMAT = "UIE_ATTEMPT_PUBLICATION1"
LOCK_FILE = "publish-attempt.lock"
HEX64 = re.compile(r"[0-9a-f]{64}")
LIMIT_FILE = 128 * 1024 * 1024
LEDGER_START = {"format", "phase", "jobId", "attempt", "verifiedSha256", "planSha256", "store", "startedAt"}
LEDGER_DONE = LEDGER_START | {"generation", "pointerSha256", "manifestSha256", "chunks", "payloadsCompared",
                              "publishedAt"}
POINTER_KEYS = {"format", "world", "dimension", "pipelineKey", "generation", "manifest", "manifestSha256"}
# Upper bound of one UIE_BLOCKS1 chunk file (batch_block_info): header, at most 65,536 runs of at most
# '[65536,65535,15],' (17 bytes) and each registry name of the snapshot at most once.
BLOCK_HEADER_BOUND = 256
RUN_BOUND = 17


class PublishAttemptError(Exception):
    """The attempt cannot be published safely; nothing was published (or see the message)."""


# ------------------------------------------------------------------ helpers

def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp(value):
    """Timezone-aware datetime, or None."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _canonical_dir(value, label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not os.path.isabs(value):
        raise PublishAttemptError(f"{label} must be an absolute path")
    path = Path(value)
    if os.path.realpath(path) != str(path) or not os.path.isdir(path):
        raise PublishAttemptError(f"{label} must be a canonical existing directory without links")
    return path


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


@contextmanager
def _locked(path: Path):
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PublishAttemptError(f"{path} is not a regular lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublishAttemptError(f"another publication holds {path}") from None
        yield
    finally:
        os.close(fd)


def _read(base: Path, relative: str, limit: int = LIMIT_FILE) -> bytes:
    try:
        return vrp._read(base, relative, limit)
    except vrp.RenderVerificationError as exc:
        raise PublishAttemptError(str(exc)) from None


def _json(base: Path, relative: str):
    try:
        return vrp._json(_read(base, relative, vrp.LIMIT_JSON), relative)
    except vrp.RenderVerificationError as exc:
        raise PublishAttemptError(str(exc)) from None


def _size(base: Path, relative: str) -> int:
    try:
        return os.lstat(vrp._path(base, relative)).st_size
    except vrp.RenderVerificationError as exc:
        raise PublishAttemptError(str(exc)) from None


# ------------------------------------------------------------------ inputs

def _verified_attempt(queue: RenderQueue, attempts: Path, job_id: str) -> dict:
    """Queue result -> attempt -> state.json -> VERIFIED.json -> stopped scope -> identical fresh report."""
    jobs = [j for j in queue.list_jobs("succeeded") if j["id"] == job_id]
    if len(jobs) != 1:
        raise PublishAttemptError("the job is not a succeeded queue job")
    result = jobs[0]["result"]
    if type(result) is not dict or set(result) != {"attempt", "verifiedSha256", "planSha256", "pipelineKey"} \
            or result["pipelineKey"] != queue.pipeline_key \
            or not all(isinstance(result[k], str) and HEX64.fullmatch(result[k]) for k in ("verifiedSha256", "planSha256")) \
            or not isinstance(result["attempt"], str) \
            or not re.fullmatch(re.escape(job_id) + r"-[1-9][0-9]*-[0-9a-f]{32}", result["attempt"]):
        raise PublishAttemptError("the succeeded job has no valid worker result")
    _, number, token = result["attempt"].rsplit("-", 2)
    number = int(number)
    if number != jobs[0]["attempt"]:
        raise PublishAttemptError("the result's attempt number is not the queue job's attempt")
    attempt = attempts / result["attempt"]
    info = os.lstat(attempt) if os.path.lexists(attempt) else None
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise PublishAttemptError("the attempt directory is missing or not a real directory")
    state = _json(attempt, ww.STATE_FILE)
    if type(state) is not dict or set(state) != ww.STATE_KEYS or state["format"] != ww.STATE_FORMAT \
            or state["phase"] != "completed" or state["jobId"] != job_id \
            or type(state["attempt"]) is not int or state["attempt"] != number or state["token"] != token \
            or state["planSha256"] != result["planSha256"] or state["planDir"] != "plan" \
            or state["queue"] != {"worldId": queue.world_id, "dimension": queue.dimension,
                                  "pipelineKey": queue.pipeline_key}:
        raise PublishAttemptError("state.json does not describe this completed attempt (job, attempt, token)")
    verified_raw = _read(attempt, ww.VERIFIED_FILE, vrp.LIMIT_JSON)
    if _sha(verified_raw) != result["verifiedSha256"]:
        raise PublishAttemptError("VERIFIED.json does not match the queue result")
    verified = vrp._json(verified_raw, ww.VERIFIED_FILE)
    if type(verified) is not dict or set(verified) != {"format", "jobId", "attempt", "planSha256", "pipelineKey",
                                                       "report"} \
            or verified["format"] != ww.VERIFIED_FORMAT or verified["jobId"] != job_id \
            or type(verified["attempt"]) is not int or verified["attempt"] != number \
            or verified["planSha256"] != result["planSha256"] or verified["pipelineKey"] != queue.pipeline_key:
        raise PublishAttemptError("VERIFIED.json does not name this job, attempt, plan and pipeline")
    if state["scope"] is not None:
        try:
            if op.inspect_owned(state["scope"]):
                raise PublishAttemptError("processes of the attempt are still running")
        except op.StaleScopeError:
            pass   # recorded on an earlier boot: none of its processes can exist
        except op.OwnedProcessError as exc:
            raise PublishAttemptError(f"cannot confirm the attempt stopped: {exc}") from None
    plan = _canonical_dir(attempt / "plan", "attempt plan")
    try:
        # Same unsupported-block policy as the queue-bound report (opt-in field, see verify_rendered_plan).
        allowed = verified["report"].get("unsupportedPolicy") == "allowed"
        report = vrp.verify_rendered_plan(plan, expected_plan_sha256=result["planSha256"], marker_name=ww.MARKER,
                                          allow_unsupported=allowed)
    except vrp.RenderVerificationError as exc:
        raise PublishAttemptError(f"fresh verification failed: {exc}") from None
    if report.get("verified") is not True or report != verified["report"] \
            or report.get("pipelineKeySource") != "declared-inputs-sha256" \
            or report.get("pipelineKey") != queue.pipeline_key:
        raise PublishAttemptError("the fresh strict report is not verified or differs from VERIFIED.json")
    if not all(isinstance(j.get("geometryFiles"), dict) and j["geometryFiles"] for j in report["perJob"]):
        # Reports from before the verifier recorded per-file hashes only carry counts: not enough to bind copies.
        raise PublishAttemptError("the report has no per-file geometry hashes; the attempt needs fresh verification")
    return {"jobId": job_id, "result": result, "attempt": attempt, "plan": plan, "report": report,
            "verifiedRaw": verified_raw, "attemptNumber": number}


# ------------------------------------------------------------------ staging

def _plan_jobs(plan: Path, report: dict) -> list:
    """[(job, info, verified geometry file hashes)] of the plan, matched to the report."""
    plan_jobs = _json(plan, "plan.json")["jobs"]
    detail = _json(plan, "jobs.json")["jobs"]
    hashes = {j["id"]: j["geometryFiles"] for j in report["perJob"]}
    if sorted(hashes) != sorted(j["id"] for j in plan_jobs) or [d["id"] for d in detail] != [j["id"] for j in plan_jobs]:
        raise PublishAttemptError("the report's jobs are not the plan's jobs")
    return [(job, info, hashes[job["id"]]) for job, info in zip(plan_jobs, detail)]


def _preflight(plan: Path, jobs: list) -> int:
    """Upper bound of all bytes staging will write, measured before anything is written."""
    bound = 0
    for job, info, files in jobs:
        base = f"results/{job['id']}/geometry"
        manifest_raw = _read(plan, f"{base}/manifest.json", vrp.LIMIT_JSON)
        if _sha(manifest_raw) != files.get("manifest.json"):
            raise PublishAttemptError(f"job {job['id']}: manifest.json changed since verification")
        manifest = vrp._json(manifest_raw, "manifest.json")
        chunks = len(manifest["chunks"])
        bound += sum(_size(plan, f"{base}/{name}") for name in [manifest["atlas"], *[c["file"] for c in manifest["chunks"]]])
        bound += 2 * len(manifest_raw) + 256 * chunks + 1024   # rewritten manifest with blockInfo entries
        bound += len(manifest_raw) + _size(plan, f"{base}/atlas-layout.json")   # capture-manifest + layout copies
        snapshot = _read(plan, job["input"], vrp.LIMIT_SNAPSHOT)
        if _sha(snapshot) != job["snapshotSha256"]:
            raise PublishAttemptError(f"job {job['id']}: the snapshot is not the verified input")
        try:
            registry = cb.parse_batch(snapshot).registry
        except Exception as exc:   # verified earlier; any parse failure now means the input changed
            raise PublishAttemptError(f"job {job['id']}: the snapshot cannot be parsed: {exc}") from None
        names = sum(len(json.dumps(str(i))) + len(json.dumps(n, ensure_ascii=False).encode("utf-8")) + 2
                    for i, n in registry.items())
        bound += chunks * (BLOCK_HEADER_BOUND + names + 65536 * RUN_BOUND)
        if isinstance(info.get("sourcePolicy"), dict) and info["sourcePolicy"].get("stagedSource") is True:
            bound += _size(plan, f"inputs/{job['id']}/staged-source.json")
    return bound


def _stage(plan: Path, stage: Path, jobs: list) -> dict:
    """Copies of each job's geometry bound to the VERIFIED per-file hashes, with block info attached.

    Returns specs, expected chunks, sources (original inputs -> SHA-256), staged (every staged file -> SHA-256)
    and outputs: the exact payload hashes the published generation must contain."""
    marker = _json(plan, ww.MARKER)["files"]
    sources = {name: marker[name] for name in ("plan.json", "jobs.json", vrp.IDENTITY_FILE) if name in marker}
    sources[ww.MARKER] = _sha(_read(plan, ww.MARKER, vrp.LIMIT_JSON))
    out = {"specs": [], "expected": [], "sources": sources, "staged": {}, "meshes": {}, "blocks": {},
           "sidecars": set(), "snapshots": [], "atlas": set()}
    for job, info, files in jobs:
        base = f"results/{job['id']}/geometry"
        manifest_raw = _read(plan, f"{base}/manifest.json", vrp.LIMIT_JSON)
        if _sha(manifest_raw) != files.get("manifest.json"):
            raise PublishAttemptError(f"job {job['id']}: manifest.json changed since verification")
        manifest = vrp._json(manifest_raw, "manifest.json")
        if _stamp(manifest.get("generatedAt")) is None:
            raise PublishAttemptError(f"job {job['id']}: manifest needs a timezone-aware generatedAt (not invented)")
        target = stage / job["id"]
        target.mkdir(mode=0o700)
        names = ["manifest.json", manifest["atlas"], *[c["file"] for c in manifest["chunks"]]]
        if set(names) | {"atlas-layout.json"} != set(files):
            raise PublishAttemptError(f"job {job['id']}: copied files are not exactly the verified geometry files")
        for name in names:
            raw = manifest_raw if name == "manifest.json" else _read(plan, f"{base}/{name}")
            if _sha(raw) != files[name]:   # bound to VERIFIED.json, not just to this read
                raise PublishAttemptError(f"job {job['id']}: {name} changed since verification")
            sources[f"{base}/{name}"] = files[name]
            with open(target / name, "xb") as stream:
                stream.write(raw)
        # Auxiliary inputs of the tracked publisher: the ORIGINAL verified capture manifest (the working copy
        # gains blockInfo below) and the verified atlas layout.
        layout_raw = _read(plan, f"{base}/atlas-layout.json")
        if _sha(layout_raw) != files["atlas-layout.json"]:
            raise PublishAttemptError(f"job {job['id']}: atlas-layout.json changed since verification")
        sources[f"{base}/atlas-layout.json"] = files["atlas-layout.json"]
        for name, raw in (("capture-manifest.json", manifest_raw), ("atlas-layout.json", layout_raw)):
            with open(target / name, "xb") as stream:
                stream.write(raw)
        out["meshes"].update({(c["x"], c["z"]): files[c["file"]] for c in manifest["chunks"]})
        out["atlas"].add(files[manifest["atlas"]])
        if marker.get(job["input"]) != job["snapshotSha256"] \
                or _sha(_read(plan, job["input"], vrp.LIMIT_SNAPSHOT)) != job["snapshotSha256"]:
            raise PublishAttemptError(f"job {job['id']}: the snapshot is not the verified input")
        sources[job["input"]] = job["snapshotSha256"]
        try:   # re-reads the snapshot and refuses unless it matches the (verified) manifest's snapshotSha256
            info_block = attach_batch_block_info(plan / job["input"], target / "manifest.json")
        except (BatchBlockInfoError, OSError) as exc:
            raise PublishAttemptError(f"job {job['id']}: block info: {exc}") from None
        out["blocks"].update({(c["x"], c["z"]): _sha((target / c["file"]).read_bytes()) for c in info_block["chunks"]})
        spec = {"geometry": str(target), "snapshotSha256": job["snapshotSha256"], "coreChunks": info["presentCoreChunks"]}
        policy = info.get("sourcePolicy")
        if isinstance(policy, dict) and policy.get("stagedSource") is True:
            relative = f"inputs/{job['id']}/staged-source.json"
            raw = _read(plan, relative, vrp.LIMIT_JSON)
            if _sha(raw) != marker.get(relative) or _sha(raw) != policy.get("stagedSourceSha256") \
                    or _sha(raw) != info.get("files", {}).get("staged-source.json"):
                raise PublishAttemptError(f"job {job['id']}: staged-source sidecar is not bound to the plan")
            sources[relative] = marker[relative]
            with open(target / "staged-source.json", "xb") as stream:
                stream.write(raw)
            spec["stagedSource"] = {"file": "staged-source.json", "sha256": _sha(raw)}
            out["sidecars"].add(_sha(raw))
        out["specs"].append(spec)
        out["expected"] += info["presentCoreChunks"]
        out["snapshots"].append(job["snapshotSha256"])
    # Every staged file (copies, rewritten manifests, generated block files) is recorded once, here.
    out["staged"] = {str(p.relative_to(stage)): _sha(p.read_bytes()) for p in stage.rglob("*") if p.is_file()}
    return out


def _rebind(plan: Path, stage: Path, staged: dict, report: dict, plan_sha: str) -> None:
    """Right before the publisher runs: staged files and original inputs still have their recorded hashes,
    the stage holds nothing else, and a strict re-verification gives the same report."""
    current = {str(p.relative_to(stage)): p for p in stage.rglob("*") if p.is_file() or p.is_symlink()}
    if set(current) != set(staged["staged"]):
        raise PublishAttemptError("the staging directory gained or lost files before publication")
    for relative, path in current.items():
        if path.is_symlink() or _sha(path.read_bytes()) != staged["staged"][relative]:
            raise PublishAttemptError(f"staged {relative} changed before publication")
    for relative, digest in staged["sources"].items():
        limit = vrp.LIMIT_SNAPSHOT if relative.endswith(".bin") else LIMIT_FILE
        if _sha(_read(plan, relative, limit)) != digest:
            raise PublishAttemptError(f"verified input {relative} changed during staging")
    try:
        again = vrp.verify_rendered_plan(plan, expected_plan_sha256=plan_sha, marker_name=ww.MARKER,
                                         allow_unsupported=report.get("unsupportedPolicy") == "allowed")
    except vrp.RenderVerificationError as exc:
        raise PublishAttemptError(f"re-verification before publication failed: {exc}") from None
    if again != report:
        raise PublishAttemptError("the attempt changed during staging (re-verification differs)")


# ------------------------------------------------------------------ store

def _check_empty(store: Path) -> None:
    if os.listdir(store):
        raise PublishAttemptError("the candidate store is not empty; this version publishes only into an "
                                  "empty store and never replaces an existing generation")


def _current_generation(store: Path):
    try:
        return json.loads(_read(store, "current.json", vrp.LIMIT_JSON))["generation"]
    except (PublishAttemptError, OSError, ValueError, KeyError, TypeError):
        return None


def _check_superset(store: Path, base: str, staged: dict) -> None:
    """Replacement only when the new generation covers every chunk of the replaced one (never a smaller cut)."""
    try:
        pointer = json.loads(_read(store, "current.json", vrp.LIMIT_JSON))
        if pointer.get("generation") != base:
            raise PublishAttemptError("the store's current generation is not expected_revision")
        manifest = json.loads(_read(store, f"generations/{base}/manifest.json", vrp.LIMIT_JSON))
        old = {(c["x"], c["z"]) for c in manifest["chunks"]}
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise PublishAttemptError(f"cannot read the generation to replace ({type(exc).__name__})") from None
    new = {tuple(c) for c in staged["expected"]}
    if not old <= new:
        raise PublishAttemptError(f"replacement would drop {len(old - new)} chunks of the current map "
                                  f"(e.g. {sorted(old - new)[:3]}); a smaller cut never replaces a larger map")


def _check_base(store: Path, base: str) -> None:
    """An update starts only from a store whose current pointer is exactly the expected tracked base."""
    if _current_generation(store) != base:
        raise PublishAttemptError("the candidate store's current generation is not expected_revision")


def _area_record(checked: dict, staged: dict, queue: RenderQueue) -> dict:
    """The area record of this attempt, built from the exact queue-bound VERIFIED.json bytes."""
    try:
        record = area_records.build_area_record(checked["plan"], checked["verifiedRaw"],
                                                verified_sha256=checked["result"]["verifiedSha256"],
                                                job_id=checked["jobId"], attempt=checked["attemptNumber"])
        return area_records.validate_area_record(record, expected={
            "pipelineKey": queue.pipeline_key, "atlasSha256": next(iter(staged["atlas"])),
            "publishedChunks": staged["expected"], "snapshots": staged["snapshots"]})
    except area_records.AreaRecordError as exc:
        raise PublishAttemptError(f"area record: {exc}") from None


def _carry_expectation(store: Path, base: str, queue: RenderQueue, area: dict) -> dict:
    """What an update on the tracked base generation must contain besides this attempt (publish_generation's
    own carry-forward plan, independently re-read): carried chunk payload hashes from the base manifest names,
    carried snapshots/policies, kept area records and the superseded tiles."""
    try:
        base_root, files, carried, kept, superseded, policies = pg._carry_plan(store, base, queue.pipeline_key, area)
    except pg.PublicationError as exc:
        raise PublishAttemptError(f"carry-forward from {base[:12]} refused: {exc}") from None
    manifest = json.loads(_read(base_root, "manifest.json", vrp.LIMIT_JSON))
    meshes, blocks = {}, {}
    for kind, entries, into in (("chunks", manifest["chunks"], meshes), ("blocks", manifest["blockInfo"]["chunks"], blocks)):
        names = {(e["x"], e["z"]): e["file"] for e in entries}
        for _, tile in carried:
            for x, z in tile["present"]:
                match = re.fullmatch(rf"{kind}/{x}_{z}-([0-9a-f]{{64}})\.json", names[(x, z)])
                if not match:
                    raise PublishAttemptError(f"base {kind} entry ({x},{z}) is not content-addressed")
                into[(x, z)] = (names[(x, z)], match.group(1))
    return {"base": base, "meshes": meshes, "blocks": blocks, "areas": kept + [area], "superseded": superseded,
            "snapshots": [t["snapshotSha256"] for _, t in carried], "policies": policies}


def _check_published(store: Path, pointer_sha: str, staged: dict, queue: RenderQueue, area: dict,
                     carry: dict | None = None, replaced: str | None = None) -> tuple:
    """The store holds exactly this attempt's expected generation: pointer bytes, identity, inventory, and every
    published payload equal to the expected hashes (meshes: VERIFIED per-file hashes; blocks and sidecars: the
    freshly staged ones), and inventory.areas exactly [this attempt's area record]. With carry (an update of a
    tracked base), the base generation may stay in the store, carried payloads must keep the base manifest's
    content-addressed names, and areas/supersededTiles/areaBase must be the carry-forward's. Returns
    (pointer, compared)."""
    try:
        entries = sorted(os.listdir(store))
        if replaced is None and entries != ["current.json", "generations"]:
            raise PublishAttemptError("the candidate store holds more than one publication")
        # A replaced (LIVE) store may keep other records (e.g. publication-audits/), never a half-done publication.
        if replaced is not None and (not {"current.json", "generations"} <= set(entries)
                                     or any(e == ".publish.lock" or e.startswith((".staging-", ".current-")) for e in entries)):
            raise PublishAttemptError("the store is not a clean publication store")
        raw = _read(store, "current.json", vrp.LIMIT_JSON)
        if _sha(raw) != pointer_sha:
            raise PublishAttemptError("the store's current pointer is not this attempt's publication")
        pointer = json.loads(raw)
        generation = pointer["generation"]
        if type(pointer) is not dict or set(pointer) != POINTER_KEYS or pointer["format"] != "UIE_REVISION1" \
                or [pointer["world"], pointer["dimension"], pointer["pipelineKey"]] != \
                [queue.world_id, queue.dimension, queue.pipeline_key] \
                or not HEX64.fullmatch(generation) or pointer["manifest"] != f"generations/{generation}/manifest.json" \
                or (carry is None and replaced is None and os.listdir(store / "generations") != [generation]) \
                or (replaced is not None and not {generation, replaced} <= set(os.listdir(store / "generations"))) \
                or (carry is not None and not {generation, carry["base"]} <= set(os.listdir(store / "generations"))):
            raise PublishAttemptError("the store pointer is not a pointer to this attempt's identity")
        root = f"generations/{generation}"
        inventory_raw = _read(store, f"{root}/COMPLETE.json", vrp.LIMIT_JSON)
        if _sha(inventory_raw) != generation:
            raise PublishAttemptError("the published inventory is damaged")
        inventory = json.loads(inventory_raw)
        files = inventory["files"]
        policies = sorted(p["sha256"] for p in inventory.get("sourcePolicies", []))
        carried_snapshots = carry["snapshots"] if carry else []
        carried_sidecars = [p["sha256"] for p in carry["policies"]] if carry else []
        if inventory["format"] != "UIE_GENERATION1" \
                or [inventory["world"], inventory["dimension"], inventory["pipelineKey"]] != \
                [pointer["world"], pointer["dimension"], pointer["pipelineKey"]] \
                or inventory["snapshots"] != sorted([*staged["snapshots"], *carried_snapshots]) \
                or policies != sorted([*staged["sidecars"], *carried_sidecars]) \
                or files.get("manifest.json") != pointer["manifestSha256"] \
                or {files.get("atlas.png")} != staged["atlas"]:
            raise PublishAttemptError("the published inventory is not this attempt's generation")
        manifest = json.loads(_read(store, pointer["manifest"], vrp.LIMIT_JSON))
        if _sha(_read(store, pointer["manifest"], vrp.LIMIT_JSON)) != pointer["manifestSha256"]:
            raise PublishAttemptError("the published manifest is damaged")
        listed = {"manifest.json", "atlas.png"} | {p["file"] for p in inventory.get("sourcePolicies", [])}
        compared = 0
        for kind, entries, expected in (("chunks", manifest["chunks"], staged["meshes"]),
                                        ("blocks", manifest["blockInfo"]["chunks"], staged["blocks"])):
            expected = dict(expected)
            for key, (name, payload_sha) in (carry[{"chunks": "meshes", "blocks": "blocks"}[kind]] if carry else {}).items():
                if key in expected:
                    raise PublishAttemptError(f"carried {kind} ({key[0]},{key[1]}) collides with this attempt")
                if next((e["file"] for e in entries if (e["x"], e["z"]) == key), None) != name:
                    raise PublishAttemptError(f"carried {kind} ({key[0]},{key[1]}) is not the base generation's file")
                expected[key] = payload_sha
            if {(e["x"], e["z"]) for e in entries} != set(expected) or len(entries) != len(expected):
                raise PublishAttemptError(f"the published {kind} set is not this attempt's")
            for e in entries:
                name = e["file"] if e["file"] in files else e["file"] + ".gz"
                stored = _read(store, f"{root}/{name}")
                if _sha(stored) != files.get(name):
                    raise PublishAttemptError(f"published file {name} is damaged")
                payload = gzip.decompress(stored) if name.endswith(".gz") else stored
                if _sha(payload) != expected[(e["x"], e["z"])]:
                    raise PublishAttemptError(f"published {kind} ({e['x']},{e['z']}) is not this attempt's payload")
                listed.add(name)
                compared += 1
        if set(files) != listed:
            raise PublishAttemptError("the published inventory lists files this attempt did not produce")
        for name in ("manifest.json", "atlas.png", *(p["file"] for p in inventory.get("sourcePolicies", []))):
            if _sha(_read(store, f"{root}/{name}")) != files[name]:
                raise PublishAttemptError(f"published file {name} is damaged")
        if "areas" not in inventory:
            raise PublishAttemptError("the published inventory has no area record (an older candidate); "
                                      "publish a fresh candidate instead")
        if carry is None:
            if canonical(inventory["areas"]) != canonical([area]) or "areaBase" in inventory \
                    or "supersededTiles" in inventory:
                raise PublishAttemptError("the published area record is not this attempt's record")
        elif canonical(inventory["areas"]) != canonical(carry["areas"]) \
                or inventory.get("supersededTiles") != carry["superseded"] or inventory.get("areaBase") != carry["base"]:
            raise PublishAttemptError("the published area records are not this update's carry-forward")
    except (KeyError, TypeError, ValueError, AttributeError, OSError) as exc:
        raise PublishAttemptError(f"the store is not a valid publication of this attempt ({type(exc).__name__})") \
            from None
    return pointer, compared


# ------------------------------------------------------------------ status and ledger

def _status(path: Path | None, state: str, message: str, published_at=None) -> None:
    if path is None:
        return
    previous = None
    if path.exists():
        try:
            previous = render_status.validate(json.loads(path.read_text(encoding="utf-8")))["lastPublishedAt"]
        except (ValueError, OSError):
            previous = None
    render_status.write_status(path, {
        "format": "UIE_RENDER_STATUS1", "state": state, "updatedAt": _now(), "completedBatches": None,
        "totalBatches": None, "etaSeconds": None, "lastPublishedAt": published_at or previous,
        "message": message[:300]})


def _published_message(generation: str, chunks: int) -> str:
    return (f"Published area generation {generation[:12]}: {chunks} chunks from 1 verified plan into a "
            f"candidate store; not the full world")


def _repair_status(path: Path | None, ledger: dict) -> bool:
    """On a repeat, make the status say what the ledger recorded (idle, its own publishedAt). True if written."""
    if path is None:
        return False
    published = _stamp(ledger["publishedAt"]).astimezone(timezone.utc)
    expected = published.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        current = render_status.validate(json.loads(path.read_text(encoding="utf-8")))
        if current["state"] == "idle" and current["lastPublishedAt"] == expected:
            return False
    except (ValueError, OSError):
        pass
    _status(path, "idle", _published_message(ledger["generation"], ledger["chunks"]), ledger["publishedAt"])
    return True


def _read_ledger(path: Path, job_id: str, result: dict, store: Path):
    """None, or an exactly valid 'published' record bound to this attempt; anything else refuses."""
    if not os.path.lexists(path):
        return None
    doc = _json(path.parent, path.name)
    keys = LEDGER_DONE if type(doc) is dict and doc.get("phase") == "published" else LEDGER_START
    valid = (type(doc) is dict and set(doc) == keys and doc["format"] == LEDGER_FORMAT
             and doc["phase"] in ("publishing", "published") and _stamp(doc["startedAt"]) is not None)
    if valid and doc["phase"] == "published":
        valid = (all(isinstance(doc[k], str) and HEX64.fullmatch(doc[k])
                     for k in ("generation", "pointerSha256", "manifestSha256"))
                 and type(doc["chunks"]) is int and doc["chunks"] > 0
                 and type(doc["payloadsCompared"]) is int and doc["payloadsCompared"] >= 0
                 and _stamp(doc["publishedAt"]) is not None)
    if not valid:
        raise PublishAttemptError("the publication ledger record is malformed; manual inspection needed")
    if [doc["jobId"], doc["attempt"], doc["verifiedSha256"], doc["planSha256"]] != \
            [job_id, result["attempt"], result["verifiedSha256"], result["planSha256"]]:
        raise PublishAttemptError("the publication ledger record belongs to another attempt; manual inspection needed")
    if doc["store"] != str(store):
        raise PublishAttemptError("this attempt was recorded for another store; manual inspection needed")
    if doc["phase"] == "publishing":
        raise PublishAttemptError("an earlier publication of this attempt was interrupted; inspect the store and "
                                  "ledger manually")
    return doc


# ------------------------------------------------------------------ API

def publish_verified_attempt(queue, attempts_dir, job_id, *, store, work_dir, ledger_dir, expected_revision=None,
                             status_file=None, compress_chunks=True, min_free_bytes=1024 ** 3,
                             max_write_bytes=None, stage_max_bytes=None, replace=False) -> dict:
    """Publish one succeeded, freshly re-verified attempt into an empty candidate store, or, with
    expected_revision, as an area update of that tracked candidate generation (whole unchanged tiles carried
    forward, see publish_generation carry_forward). With replace=True and expected_revision, the attempt becomes a
    complete new generation that REPLACES that revision (any, also untracked): it must cover every chunk of the
    replaced generation (a smaller cut never replaces a larger map); older generations stay. See PUBLISH-ATTEMPT.md."""
    if type(replace) is not bool or (replace and expected_revision is None):
        raise PublishAttemptError("replace must be a boolean and needs expected_revision")
    if not isinstance(queue, RenderQueue):
        raise PublishAttemptError("queue must be a RenderQueue")
    if not isinstance(job_id, str) or not HEX64.fullmatch(job_id):
        raise PublishAttemptError("job_id must be 64 lowercase hex")
    if expected_revision is not None and (not isinstance(expected_revision, str) or not HEX64.fullmatch(expected_revision)):
        raise PublishAttemptError("expected_revision must be None or 64 lowercase hex")
    for name, value, optional in (("min_free_bytes", min_free_bytes, False), ("max_write_bytes", max_write_bytes, True),
                                  ("stage_max_bytes", stage_max_bytes, True)):
        if (value is None and not optional) or (value is not None and (type(value) is not int or value < 0)):
            raise PublishAttemptError(f"{name} must be a non-negative integer")
    attempts = _canonical_dir(attempts_dir, "attempts_dir")
    store = _canonical_dir(store, "store")
    work = _canonical_dir(work_dir, "work_dir")
    ledger_root = _canonical_dir(ledger_dir, "ledger_dir")
    roots = {"attempts_dir": attempts, "store": store, "work_dir": work, "ledger_dir": ledger_root}
    names = list(roots)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if _overlap(roots[a], roots[b]):
                raise PublishAttemptError(f"{a} and {b} overlap")
    status = None
    if status_file is not None:
        if not isinstance(status_file, (str, os.PathLike)) or not os.path.isabs(status_file):
            raise PublishAttemptError("status_file must be an absolute path")
        status = Path(status_file)
        _canonical_dir(status.parent, "status_file directory")
        if any(_overlap(status, root) or root in status.parents for root in roots.values()):
            raise PublishAttemptError("status_file must lie outside the store, attempts, work and ledger dirs")

    with _locked(ledger_root / LOCK_FILE):
        checked = _verified_attempt(queue, attempts, job_id)
        result = checked["result"]
        ledger_path = ledger_root / f"{result['verifiedSha256']}.json"
        ledger = _read_ledger(ledger_path, job_id, result, store)
        if ledger is None and expected_revision is None:
            _check_empty(store)
        elif ledger is None:
            _check_base(store, expected_revision)
        jobs = _plan_jobs(checked["plan"], checked["report"])
        bound = _preflight(checked["plan"], jobs)
        if stage_max_bytes is not None and bound > stage_max_bytes:
            raise PublishAttemptError(f"staging needs up to {bound} bytes, more than stage_max_bytes")
        if shutil.disk_usage(work).free < bound + min_free_bytes:
            raise PublishAttemptError("staging would exhaust the free disk reserve of work_dir")
        stage = work / f".publish-{result['verifiedSha256'][:16]}-{uuid.uuid4().hex}"
        os.mkdir(stage, 0o700)
        cleanup_error = None
        try:
            outcome = _run(queue, checked, jobs, stage, bound, store, ledger, ledger_path, status,
                           compress_chunks, min_free_bytes, max_write_bytes, expected_revision, replace)
        finally:
            try:
                if stage.parent == work and stage.name.startswith(".publish-") and not stage.is_symlink():
                    shutil.rmtree(stage)
            except OSError as exc:   # never hides the original error; reported on success
                cleanup_error = str(exc)
        if cleanup_error:
            outcome["stageNotRemoved"] = cleanup_error
        return outcome


def _run(queue, checked, jobs, stage, bound, store, ledger, ledger_path, status, compress_chunks,
         min_free_bytes, max_write_bytes, base=None, replace=False) -> dict:
    result, plan = checked["result"], checked["plan"]
    staged = _stage(plan, stage, jobs)
    stage_bytes = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file())
    if stage_bytes > bound:
        raise PublishAttemptError("staging wrote more than its preflight bound")
    area = _area_record(checked, staged, queue)
    carry = _carry_expectation(store, base, queue, area) if base is not None and not replace else None
    replaced = base if replace else None
    if replace and ledger is None:   # a repeat is proven by _check_published (the pointer has moved on by then)
        _check_superset(store, base, staged)
    expected = sorted(map(list, {tuple(c) for c in staged["expected"]} | set(carry["meshes"] if carry else ())),
                      key=lambda c: (c[1], c[0]))
    if ledger is not None:   # repeat: prove the store is exactly this attempt's generation
        pointer, compared = _check_published(store, ledger["pointerSha256"], staged, queue, area, carry, replaced)
        if [pointer["generation"], pointer["manifestSha256"], ledger["chunks"]] != \
                [ledger["generation"], ledger["manifestSha256"], len(expected)]:
            raise PublishAttemptError("the ledger record does not describe the published generation")
        repaired = _repair_status(status, ledger)
        return {"outcome": "already-published", "generation": pointer["generation"], "store": str(store),
                "chunks": ledger["chunks"], "payloadsCompared": compared, "statusRepaired": repaired}

    _rebind(plan, stage, staged, checked["report"], result["planSha256"])   # before any pointer exists
    record = {"format": LEDGER_FORMAT, "phase": "publishing", "jobId": checked["jobId"],
              "attempt": result["attempt"], "verifiedSha256": result["verifiedSha256"],
              "planSha256": result["planSha256"], "store": str(store), "startedAt": _now()}
    chunks = len(expected)
    # Status first: a failing status write must not leave a 'publishing' record behind.
    _status(status, "publishing", f"Publishing verified area {result['planSha256'][:12]} ({chunks} chunks) "
                                  f"into a candidate store")
    try:
        ww._write_json(ledger_path, record)   # durable BEFORE the store can change
    except Exception as exc:
        _status(status, "error", "Publication not started: the ledger record could not be written")
        raise PublishAttemptError(f"the ledger record could not be written; nothing was published: {exc}") from None
    try:
        extra = {} if base is None or replace else {"carry_forward": {"generation": base}}
        before = None if base is None else sorted(os.listdir(store / "generations"))
        pointer = publish_generation(staged["specs"], store, world=queue.world_id, dimension=queue.dimension,
                                     pipeline_key=queue.pipeline_key, expected_chunks=expected,
                                     expected_revision=base, compress_chunks=compress_chunks,
                                     min_free_bytes=min_free_bytes, max_write_bytes=max_write_bytes,
                                     area_record=area, **extra)
    except Exception as exc:
        if (base is None and not os.listdir(store)) or (base is not None and _current_generation(store) == base
                                                   and sorted(os.listdir(store / "generations")) == before):
            os.unlink(ledger_path)   # nothing reached the store: a retry may start over
            _status(status, "error", f"Publication refused: {type(exc).__name__}")
            raise PublishAttemptError(f"publication failed, the store is unchanged: {exc}") from None
        _status(status, "error", "Publication interrupted; candidate store needs manual inspection")
        raise PublishAttemptError(f"publication failed after the store changed; the ledger stays "
                                  f"'publishing' for manual inspection: {exc}") from None
    try:   # the store has changed now: any doubt leaves the ledger 'publishing' (fail closed)
        pointer_sha = _sha((store / "current.json").read_bytes())
        if pointer_sha != _sha(canonical(pointer)):
            raise PublishAttemptError("the store pointer is not the one just published")
        _, compared = _check_published(store, pointer_sha, staged, queue, area, carry, replaced)
        for relative, digest in staged["sources"].items():
            if _sha(_read(plan, relative)) != digest:
                raise PublishAttemptError(f"verified input {relative} changed during publication")
    except Exception as exc:
        _status(status, "error", "Published candidate failed its post-check; manual inspection needed")
        raise PublishAttemptError(f"post-publication check failed; the ledger stays 'publishing': {exc}") from None
    published_at = _now()
    ww._write_json(ledger_path, {**record, "phase": "published", "generation": pointer["generation"],
                                 "pointerSha256": pointer_sha, "manifestSha256": pointer["manifestSha256"],
                                 "chunks": chunks, "payloadsCompared": compared, "publishedAt": published_at})
    _status(status, "idle", _published_message(pointer["generation"], chunks), published_at)
    return {"outcome": "published", "generation": pointer["generation"], "store": str(store), "chunks": chunks,
            "payloadsCompared": compared, "stageBytes": stage_bytes, "stageBound": bound}

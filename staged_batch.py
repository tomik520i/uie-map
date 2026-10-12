"""Explicit hand-off of ONE staged saved tile (UIE_SAVED_TILE1) to a UIE2 batch, under a caller-accepted policy.

create_staged_batch(staged_dir, output_dir, *, tile_x, tile_z, accept_consistency) -> staged-source record

The staged tile (stage_saved_tile.py) is an immutable byte collection whose consistency is only
"observed-stable-files": every file was stable while read and unchanged on re-read, but the files were
read at different moments from a possibly live world. It is NOT an atomic or offline-consistent world
snapshot, and the immutability of the local copy does not change that.

capture_batch_file.create_batch documents that it expects an offline, consistent copy. This wrapper does
not weaken or change that function. The caller must pass accept_consistency="observed-stable-files"
(there is no default) to state that a best-effort map export from such a collection is acceptable. The
decision is recorded next to the batch in staged-source.json and bound into its COMPLETE marker.

Steps (nothing is queued, rendered or sent anywhere):
1. The staged tile is verified: exact directory contents (COMPLETE, provenance.json, level.dat, region/
   with exactly the present regions), bounded COMPLETE and provenance, provenance SHA-256, strict schema,
   tile/input/core equal to plan_regions(tile_x, tile_z), and SHA-256/size of every staged file.
2. create_batch builds the UIE2 batch from the staged level.dat and region/ into this run's own sibling
   work directory. The staged tile is verified again; any difference fails the run.
3. The batch is checked (exact files, COMPLETE hashes, provenance bound to the staged level.dat/regions).
4. The output is reserved with an exclusive mkdir. snapshot.bin, snapshot.tiles.json, provenance.json and
   staged-source.json are moved in and re-hashed; COMPLETE goes in last. It keeps every field of the
   original UIE2_BATCH_COMPLETE1 marker and adds "stagedSource": {"file", "sha256"}.
On failure only this run's work directory, the files it moved and the output directory it created are
removed. Source reads are bounded (level.dat 16 MiB, regions 256 MiB, markers/provenance 1 MiB).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from datetime import datetime
from pathlib import Path

import capture_batch_file as cbf
import stage_saved_tile as sst

ACCEPTED_CONSISTENCY = "observed-stable-files"
FORMAT = "UIE_STAGED_SOURCE1"
SIDECAR_FILE = "staged-source.json"
MAX_MARKER_BYTES = 1024 * 1024
MAX_BATCH_FILE_BYTES = 256 * 1024 * 1024
BLOCK = 1 << 20
HEX64 = re.compile(r"[0-9a-f]{64}")
STAGED_ENTRIES = {sst.LEVEL_FILE, "region", sst.PROVENANCE_FILE, sst.COMPLETE_FILE}
BATCH_DATA = (cbf.SNAPSHOT_FILE, cbf.TILES_FILE, cbf.PROVENANCE_FILE)
PROVENANCE_KEYS = {"format", "consistency", "consistencyNote", "atomicWorldSnapshot", "tile", "input", "core",
                   "sourceInterval", "sources", "regions", "files"}
METADATA_KEYS = {"size", "mtimeNs", "ctimeNs", "device", "inode"}
WARNING = ("Built from a staged saved tile whose files were each observed stable while read and unchanged on "
           "re-read, but read at different moments from a possibly live world. This batch is a best-effort map "
           "export explicitly accepted by the caller; it is not a consistent world snapshot, and the immutability "
           "of the staged copy does not make the source consistent.")


class StagedBatchError(cbf.BatchFileError):
    """The staged tile cannot be converted safely; no complete output exists."""


# ---------------------------------------------------------------- safe bounded reads

def _redirected(info) -> bool:
    """A symlink, or on Windows any reparse point (junction, mount point, cloud placeholder)."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _refuse_redirection(path: Path, label: str) -> None:
    """path and every existing ancestor must be real entries (lstat only; casing is never compared)."""
    for entry in (path, *path.parents):
        try:
            info = os.lstat(entry)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise StagedBatchError(f"{label}: cannot inspect {entry} ({exc})") from None
        if _redirected(info):
            raise StagedBatchError(f"{label}: {entry} is a symlink or reparse point; pass the real path")


def _listing(directory: Path, label: str) -> dict:
    """{name: lstat} of a real directory; no entry may be a link."""
    try:
        entries = {entry.name: os.lstat(entry.path) for entry in os.scandir(directory)}
    except OSError as exc:
        raise StagedBatchError(f"{label}: cannot list ({exc})") from None
    for name, info in entries.items():
        if _redirected(info):
            raise StagedBatchError(f"{label}/{name} is a symlink or reparse point")
    return entries


def _open(path: Path, limit: int, label: str):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise StagedBatchError(f"{label}: missing") from None
    if _redirected(info) or not stat.S_ISREG(info.st_mode):
        raise StagedBatchError(f"{label}: not a regular file")
    stream = os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)), "rb")
    if os.fstat(stream.fileno()).st_size > limit:
        stream.close()
        raise StagedBatchError(f"{label}: larger than {limit} bytes")
    return stream


def _read_bytes(path: Path, limit: int, label: str) -> bytes:
    with _open(path, limit, label) as stream:
        # Read the actual size (+1 to detect growth): read(limit + 1) preallocates the whole limit on Windows.
        size = os.fstat(stream.fileno()).st_size
        raw = stream.read(size + 1)
        if len(raw) > size:
            raw += stream.read(limit + 1 - len(raw))
    if len(raw) > limit:
        raise StagedBatchError(f"{label}: larger than {limit} bytes")
    return raw


def _hash_file(path: Path, limit: int, label: str) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with _open(path, limit, label) as stream:
        while block := stream.read(BLOCK):
            size += len(block)
            if size > limit:
                raise StagedBatchError(f"{label}: larger than {limit} bytes")
            digest.update(block)
    return digest.hexdigest(), size


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StagedBatchError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _no_constant(name):
    raise StagedBatchError(f"non-finite JSON number {name}")


def _json(raw: bytes, label: str):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StagedBatchError(f"{label}: not UTF-8 JSON ({exc})") from None


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


def _is_int(value) -> bool:
    return type(value) is int


def _is_sha(value) -> bool:
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


# ---------------------------------------------------------------- staged tile

def _check_metadata(value, size: int, label: str) -> None:
    if not isinstance(value, dict) or not {"size", "mtimeNs"} <= set(value) <= METADATA_KEYS \
            or not all(_is_int(v) for v in value.values()) or value["size"] != size:
        raise StagedBatchError(f"{label}: invalid observedMetadata")


def _timestamp(value, label: str) -> datetime:
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise StagedBatchError(f"{label} is not an ISO 8601 timestamp") from None
    if when.tzinfo is None:
        raise StagedBatchError(f"{label} needs a timezone")
    return when


def _check_provenance(p, tile_x: int, tile_z: int, area, core, regions) -> None:
    if not isinstance(p, dict) or set(p) != PROVENANCE_KEYS:
        raise StagedBatchError("staged provenance does not have the UIE_SAVED_TILE1 fields")
    if p["format"] != sst.FORMAT:
        raise StagedBatchError(f"staged provenance format is {p['format']!r}")
    if p["consistency"] != ACCEPTED_CONSISTENCY or p["atomicWorldSnapshot"] is not False:
        raise StagedBatchError("staged provenance must be observed-stable-files with atomicWorldSnapshot false")
    if not isinstance(p["consistencyNote"], str) or not p["consistencyNote"]:
        raise StagedBatchError("staged provenance has no consistency note")
    tile = p["tile"]
    if not isinstance(tile, dict) or set(tile) != {"x", "z"} or not all(_is_int(v) for v in tile.values()) \
            or (tile["x"], tile["z"]) != (tile_x, tile_z):
        raise StagedBatchError(f"staged tile {tile} is not the requested tile ({tile_x}, {tile_z})")
    as_dict = lambda a: {"x": a.x, "z": a.z, "width": a.width, "depth": a.depth}
    if any(not isinstance(p[k], dict) or not all(_is_int(v) for v in p[k].values()) for k in ('input','core')) \
            or p["input"] != as_dict(area) or p["core"] != as_dict(core):
        raise StagedBatchError("staged input/core differ from the planned tile")
    interval = p["sourceInterval"]
    if not isinstance(interval, dict) or set(interval) != {"startUtc", "endUtc"} \
            or _timestamp(interval["startUtc"], "startUtc") > _timestamp(interval["endUtc"], "endUtc"):
        raise StagedBatchError("staged sourceInterval is invalid")
    sources = p["sources"]
    if not isinstance(sources, dict) or set(sources) != {"levelDat", "regionDir"} or not isinstance(sources["regionDir"], str):
        raise StagedBatchError("staged sources are invalid")
    level = sources["levelDat"]
    if not isinstance(level, dict) or set(level) != {"path", "sha256", "size", "observedMetadata"} \
            or not isinstance(level["path"], str) or not _is_sha(level["sha256"]) or not _is_int(level["size"]):
        raise StagedBatchError("staged levelDat record is invalid")
    _check_metadata(level["observedMetadata"], level["size"], "levelDat")
    entries = p["regions"]
    if not isinstance(entries, list) or [(e.get("x"), e.get("z")) if isinstance(e, dict) else None
                                         for e in entries] != list(regions):
        raise StagedBatchError("staged regions differ from the planned tile regions")
    expected_files = {sst.LEVEL_FILE: level["sha256"]}
    for e in entries:
        if not _is_int(e.get('x')) or not _is_int(e.get('z')):
            raise StagedBatchError('region coordinates must be integers')
        if e.get("state") == "absent":
            if set(e) != {"x", "z", "state"}:
                raise StagedBatchError(f"absent region {e['x']},{e['z']} has unexpected fields")
            continue
        if e.get("state") != "present" or set(e) != {"x", "z", "state", "file", "sha256", "size", "observedMetadata"} \
                or e["file"] != f"region/r.{e['x']}.{e['z']}.mca" or not _is_sha(e["sha256"]) or not _is_int(e["size"]):
            raise StagedBatchError(f"region {e.get('x')},{e.get('z')} record is invalid")
        _check_metadata(e["observedMetadata"], e["size"], e["file"])
        expected_files[e["file"]] = e["sha256"]
    if p["files"] != expected_files:
        raise StagedBatchError("staged files map does not match levelDat and the present regions")


def verify_staged_tile(staged: Path, tile_x: int, tile_z: int) -> dict:
    """Full check of a staged tile; returns hashes of COMPLETE/provenance, the provenance and the inventory."""
    area, core, regions = sst.plan_regions(tile_x, tile_z)
    top = _listing(staged, "staged tile")
    if set(top) != STAGED_ENTRIES:
        raise StagedBatchError(f"staged tile must contain exactly {sorted(STAGED_ENTRIES)}, found {sorted(top)}")
    if not stat.S_ISDIR(top["region"].st_mode):
        raise StagedBatchError("staged region is not a directory")
    complete_raw = _read_bytes(staged / sst.COMPLETE_FILE, MAX_MARKER_BYTES, "staged COMPLETE")
    complete = _json(complete_raw, "staged COMPLETE")
    if not isinstance(complete, dict) or set(complete) != {"format", "provenanceSha256"} \
            or complete["format"] != sst.FORMAT or not _is_sha(complete["provenanceSha256"]):
        raise StagedBatchError("staged COMPLETE is not a UIE_SAVED_TILE1 marker")
    provenance_raw = _read_bytes(staged / sst.PROVENANCE_FILE, MAX_MARKER_BYTES, "staged provenance")
    if _sha(provenance_raw) != complete["provenanceSha256"]:
        raise StagedBatchError("staged provenance does not match COMPLETE.provenanceSha256")
    provenance = _json(provenance_raw, "staged provenance")
    _check_provenance(provenance, tile_x, tile_z, area, core, regions)
    present = [e for e in provenance["regions"] if e["state"] == "present"]
    names = set(_listing(staged / "region", "staged region"))
    expected_names = {e["file"].split("/", 1)[1] for e in present}
    if names != expected_names:
        raise StagedBatchError(f"staged region files {sorted(names)} differ from the present regions "
                               f"{sorted(expected_names)} (missing, extra or appeared absent region)")
    level = provenance["sources"]["levelDat"]
    inventory = {sst.LEVEL_FILE: _hash_file(staged / sst.LEVEL_FILE, sst.MAX_LEVEL_BYTES, "staged level.dat")}
    if inventory[sst.LEVEL_FILE] != (level["sha256"], level["size"]):
        raise StagedBatchError("staged level.dat does not match its provenance")
    for e in present:
        inventory[e["file"]] = _hash_file(staged / e["file"], sst.MAX_REGION_BYTES, f"staged {e['file']}")
        if inventory[e["file"]] != (e["sha256"], e["size"]):
            raise StagedBatchError(f"staged {e['file']} does not match its provenance")
    return {"completeSha256": _sha(complete_raw), "provenanceSha256": complete["provenanceSha256"],
            "provenance": provenance, "inventory": inventory}


# ---------------------------------------------------------------- built batch

def _check_batch(batch: Path, staged: dict) -> tuple[dict, dict]:
    """(original COMPLETE, batch provenance) of a UIE2 batch built from exactly the staged bytes."""
    top = _listing(batch, "built batch")
    if set(top) != {*BATCH_DATA, cbf.COMPLETE_FILE}:
        raise StagedBatchError(f"built batch has unexpected files {sorted(top)}")
    complete = _json(_read_bytes(batch / cbf.COMPLETE_FILE, MAX_MARKER_BYTES, "batch COMPLETE"), "batch COMPLETE")
    if not isinstance(complete, dict) or complete.get("format") != "UIE2_BATCH_COMPLETE1" \
            or not isinstance(complete.get("files"), dict) or set(complete["files"]) != set(BATCH_DATA) \
            or complete.get("snapshotSha256") != complete["files"][cbf.SNAPSHOT_FILE] or "stagedSource" in complete:
        raise StagedBatchError("built batch COMPLETE is not a UIE2_BATCH_COMPLETE1 marker")
    for name in BATCH_DATA:
        if _hash_file(batch / name, MAX_BATCH_FILE_BYTES, f"batch {name}")[0] != complete["files"][name]:
            raise StagedBatchError(f"built batch {name} does not match its COMPLETE")
    provenance = _json(_read_bytes(batch / cbf.PROVENANCE_FILE, MAX_BATCH_FILE_BYTES, "batch provenance"),
                       "batch provenance")
    level = provenance.get("levelDat") if isinstance(provenance, dict) else None
    if not isinstance(level, dict) or (level.get("sha256"), level.get("size")) != staged["inventory"][sst.LEVEL_FILE]:
        raise StagedBatchError("batch provenance is not bound to the staged level.dat")
    built = {tuple(r.get("region", ())): r for r in provenance.get("regions", []) if isinstance(r, dict)}
    for e in staged["provenance"]["regions"]:
        r = built.get((e["x"], e["z"]))
        if r is None or r.get("state") != e["state"] or (
                e["state"] == "present" and (r.get("sha256"), r.get("size")) != (e["sha256"], e["size"])):
            raise StagedBatchError(f"batch provenance region {e['x']},{e['z']} is not bound to the staged tile")
    if len(built) != len(staged["provenance"]["regions"]):
        raise StagedBatchError("batch provenance lists other regions than the staged tile")
    return complete, provenance


def _write_new(path: Path, raw: bytes) -> None:
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


# ---------------------------------------------------------------- hand-off

def create_staged_batch(staged_dir, output_dir, *, tile_x, tile_z, accept_consistency) -> dict:
    if type(accept_consistency) is not str or accept_consistency != ACCEPTED_CONSISTENCY:
        raise StagedBatchError(f"accept_consistency must be exactly {ACCEPTED_CONSISTENCY!r}: the caller explicitly "
                               "accepts a best-effort map export from a non-atomic staged tile")
    try:
        area, core, _ = sst.plan_regions(tile_x, tile_z)
    except sst.StageError as exc:
        raise StagedBatchError(str(exc)) from None
    staged, output = Path(os.path.abspath(staged_dir)), Path(os.path.abspath(output_dir))
    _refuse_redirection(staged, "staged tile")
    _refuse_redirection(output, "output")
    if not os.path.isdir(staged):
        raise StagedBatchError(f"staged tile {staged} is not a directory")
    staged_real, output_real = staged.resolve(), output.resolve()
    if _within(output_real, staged_real) or _within(staged_real, output_real):
        raise StagedBatchError("output overlaps the staged tile")
    if os.path.lexists(output):
        raise StagedBatchError(f"output {output} already exists; it is never reused or overwritten")
    before = verify_staged_tile(staged, tile_x, tile_z)
    # Never write next to the live world the tile was staged from.
    origins = before["provenance"]["sources"]
    for origin in (Path(origins["levelDat"]["path"]).parent, Path(origins["regionDir"])):
        if _within(output_real, origin.resolve()):
            raise StagedBatchError("output lies inside the original world the tile was staged from")

    output.parent.mkdir(parents=True, exist_ok=True)
    work = output.parent / f".{output.name}.staged-batch-{uuid.uuid4().hex}"
    os.mkdir(work)
    created_output = False
    moved: list[Path] = []
    success = False
    try:
        built = work / "batch"
        cbf.create_batch(staged / sst.LEVEL_FILE, staged / "region", area, core, built)
        after = verify_staged_tile(staged, tile_x, tile_z)
        if after != before:
            raise StagedBatchError("the staged tile changed while the batch was built")
        complete, _ = _check_batch(built, before)

        p = before["provenance"]
        record = {
            "format": FORMAT,
            "policy": {"acceptedConsistency": ACCEPTED_CONSISTENCY, "atomicWorldSnapshot": False,
                       "purpose": "best-effort map export explicitly accepted by the caller"},
            "warning": WARNING,
            "tile": p["tile"], "input": p["input"], "core": p["core"],
            "stagedTile": {
                "path": str(staged), "completeSha256": before["completeSha256"],
                "provenanceSha256": before["provenanceSha256"], "consistency": p["consistency"],
                "consistencyNote": p["consistencyNote"], "atomicWorldSnapshot": p["atomicWorldSnapshot"],
                "sourceInterval": p["sourceInterval"],
                "levelDat": {"sha256": p["sources"]["levelDat"]["sha256"], "size": p["sources"]["levelDat"]["size"]},
                "regions": [{k: e[k] for k in ("x", "z", "state", "sha256", "size") if k in e} for e in p["regions"]],
            },
            "batch": {"snapshotSha256": complete["snapshotSha256"], "files": complete["files"]},
        }
        record_bytes = _json_bytes(record)
        # Original UIE2 marker fields are kept unchanged; the policy sidecar is bound by its hash.
        marker_bytes = _json_bytes({**complete, "stagedSource": {"file": SIDECAR_FILE, "sha256": _sha(record_bytes)}})
        _write_new(work / SIDECAR_FILE, record_bytes)
        _write_new(work / "COMPLETE.staged", marker_bytes)
        expected = {**{name: complete["files"][name] for name in BATCH_DATA}, SIDECAR_FILE: _sha(record_bytes)}

        try:
            os.mkdir(output)  # exclusive reservation; COMPLETE inside it is what marks a batch
        except FileExistsError:
            raise StagedBatchError(f"output {output} appeared during the run; nothing was overwritten") from None
        created_output = True
        for name in expected:
            source = work / SIDECAR_FILE if name == SIDECAR_FILE else built / name
            if os.path.lexists(output / name):
                raise StagedBatchError(f"unexpected {name} in the reserved output")
            os.replace(source, output / name)
            moved.append(output / name)
            if _hash_file(output / name, MAX_BATCH_FILE_BYTES, name)[0] != expected[name]:
                raise StagedBatchError(f"{name} changed while it was published")
        _sync_dir(output)
        if os.path.lexists(output / cbf.COMPLETE_FILE):
            raise StagedBatchError("unexpected COMPLETE in the reserved output")
        os.replace(work / "COMPLETE.staged", output / cbf.COMPLETE_FILE)  # commit marker, last
        moved.append(output / cbf.COMPLETE_FILE)
        _sync_dir(output)
        success = True
        return record
    finally:
        try:
            if not success:
                # Only what this run created.
                for path in reversed(moved):
                    path.unlink(missing_ok=True)
                if created_output:
                    try:
                        os.rmdir(output)
                    except OSError:
                        pass  # something foreign appeared inside; leave it untouched
        finally:
            if work.exists() and not work.is_symlink() and work.name.startswith(f".{output.name}.staged-batch-"):
                shutil.rmtree(work)

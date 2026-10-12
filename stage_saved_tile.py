"""Bounded staging of the saved-world source bytes of ONE planned tile (UIE_SAVED_TILE1).

stage_saved_tile(level_dat, region_dir, output_dir, *, tile_x, tile_z,
                 min_free_bytes=1 GiB, max_write_bytes=512 MiB) -> provenance

Copies level.dat and only the region files touched by the tile's 8x8-chunk input window
(capture_batch_file.tile_areas + capture_snapshot.plan_chunks: at most 4 regions), never the whole world.
Nothing is parsed, rendered or queued: an existing but corrupt region is copied byte for byte and parsing
is left to create_batch downstream.

Consistency is exactly "observed-stable-files": each file was read once in bounded blocks, its size and
mtime did not change during that read, and after all copies every present source was re-read with the
same SHA-256 and size and every absent region was still absent. This is NOT an atomic world snapshot.
The files are read at different moments; a live server can keep unsaved chunks in memory or save between
reads, so the collection is not guaranteed to be globally consistent. Staging does not make a live world
coherent and does not change create_batch's offline-copy contract; the purpose is immutable bytes for
inspection, retry and a later explicit policy. One attempt only: a detected change fails, the caller
decides whether to retry.

Output directory (must not exist; reserved with an exclusive mkdir):
    level.dat, region/r.<x>.<z>.mca    exact source bytes
    provenance.json                    UIE_SAVED_TILE1 record (tile, input/core, regions incl. absent,
                                       source paths/digests/sizes, UTC source interval, consistency note)
    COMPLETE                           written last; only a directory with COMPLETE is a staged tile
Files are built in a fresh sibling ".<name>.stage-<uuid>" directory. Sources are never written, renamed
or deleted; only this run's temporary directory and the output files/directories it created are removed.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

import capture_batch_file as cbf
import capture_snapshot as cs

FORMAT = "UIE_SAVED_TILE1"
CONSISTENCY = "observed-stable-files"
CONSISTENCY_NOTE = ("Each copied file was stable during its own read and unchanged when re-read after all copies; "
                    "absent regions stayed absent. This is not an atomic world snapshot: files were read at "
                    "different moments and a live server may hold unsaved chunks or save between reads, so the "
                    "collection is not guaranteed to be globally consistent.")
MAX_LEVEL_BYTES = 16 * 1024 * 1024
MAX_REGION_BYTES = 256 * 1024 * 1024
MAX_REGIONS = 4
BLOCK = 1 << 20
LEVEL_FILE = "level.dat"
PROVENANCE_FILE = "provenance.json"
COMPLETE_FILE = "COMPLETE"


class StageError(cs.SnapshotError):
    """The tile source cannot be staged safely; no complete output exists."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _area(area: cs.Area) -> dict:
    return {"x": area.x, "z": area.z, "width": area.width, "depth": area.depth}


def plan_regions(tile_x: int, tile_z: int) -> tuple[cs.Area, cs.Area, list[tuple[int, int]]]:
    """(input window, core, region coordinates sorted by z then x) of one planned tile."""
    try:
        area, core = cbf.tile_areas(tile_x, tile_z)
    except cs.SnapshotError as exc:
        raise StageError(str(exc)) from None
    regions = sorted(cs.plan_chunks(area), key=lambda r: (r[1], r[0]))
    if not 1 <= len(regions) <= MAX_REGIONS:
        raise StageError(f"tile touches {len(regions)} regions, expected 1..{MAX_REGIONS}")
    return area, core, regions


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
            raise StageError(f"{label}: cannot inspect {entry} ({exc})") from None
        if _redirected(info):
            raise StageError(f"{label}: {entry} is a symlink or reparse point; pass the real path")


def _open_source(path: Path, limit: int, label: str):
    """Open a regular, non-redirected source file of at most limit bytes. lstat and open are separate steps;
    a concurrent swap between them is detected on POSIX (st_dev/st_ino), not prevented."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise StageError(f"{label}: missing") from None
    if _redirected(info) or not stat.S_ISREG(info.st_mode):
        raise StageError(f"{label}: not a regular file (links are refused)")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    stream = os.fdopen(fd, "rb")
    try:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (
                os.name == "posix" and (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
            raise StageError(f"{label}: changed while it was opened")
        if opened.st_size > limit:
            raise StageError(f"{label}: larger than {limit} bytes")
    except BaseException:
        stream.close()
        raise
    return stream, opened


def _copy_file(source: Path, target: Path, limit: int, label: str, reserve) -> dict:
    """Streaming bounded copy with pre/post fd metadata; the SHA-256 is of the copied bytes."""
    stream, before = _open_source(source, limit, label)
    with stream:
        reserve(before.st_size)
        digest, size = hashlib.sha256(), 0
        target.parent.mkdir(exist_ok=True)
        with open(target, "xb") as out:
            while block := stream.read(BLOCK):
                size += len(block)
                if size > before.st_size:
                    raise StageError(f"{label}: grew while it was copied")
                digest.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        after = os.fstat(stream.fileno())
    if size != before.st_size or _metadata(after) != _metadata(before):
        raise StageError(f"{label}: changed while it was copied")
    return {"sha256": digest.hexdigest(), "size": size, "observedMetadata": _metadata(before)}


def _metadata(info) -> dict:
    result = {"size": info.st_size, "mtimeNs": info.st_mtime_ns}
    if os.name == "posix":
        result.update(ctimeNs=info.st_ctime_ns, device=info.st_dev, inode=info.st_ino)
    return result


def _hash_source(source: Path, limit: int, label: str, expected_metadata=None) -> tuple[str, int]:
    stream, before = _open_source(source, limit, label)
    digest, size = hashlib.sha256(), 0
    with stream:
        if expected_metadata is not None and _metadata(before) != expected_metadata:
            raise StageError(f"{label} changed before verification read")
        while block := stream.read(BLOCK):
            size += len(block)
            if size > min(limit, before.st_size):
                raise StageError(f"{label}: changed during verification read")
            digest.update(block)
        after = os.fstat(stream.fileno())
    if size != before.st_size or _metadata(after) != _metadata(before):
        raise StageError(f"{label}: changed during verification read")
    return digest.hexdigest(), size


def _hash_copy(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while block := stream.read(BLOCK):
            digest.update(block)
    return digest.hexdigest()


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


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


def stage_saved_tile(level_dat, region_dir, output_dir, *, tile_x, tile_z,
                     min_free_bytes=1024 ** 3, max_write_bytes=512 * 1024 ** 2) -> dict:
    for name, value in (("min_free_bytes", min_free_bytes), ("max_write_bytes", max_write_bytes)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise StageError(f"{name} must be a non-negative integer")
    area, core, regions = plan_regions(tile_x, tile_z)

    level_dat, region_dir, output = (Path(os.path.abspath(p)) for p in (level_dat, region_dir, output_dir))
    for path, label in ((level_dat, "level.dat"), (region_dir, "region directory"), (output, "output")):
        _refuse_redirection(path, label)
    try:
        if not stat.S_ISREG(os.lstat(level_dat).st_mode):
            raise StageError("level.dat is not a regular file")
        if not stat.S_ISDIR(os.lstat(region_dir).st_mode):
            raise StageError("region directory is not a directory")
    except FileNotFoundError as exc:
        raise StageError(f"source is missing ({exc.filename})") from None
    # The output must be outside the world folder and the region directory, and must not contain them.
    output_real = output.resolve()
    for source in (level_dat.parent.resolve(), region_dir.resolve()):
        if output_real == source or output_real.is_relative_to(source) or source.is_relative_to(output_real):
            raise StageError("output overlaps the source world or region directory")
    if os.path.lexists(output):
        raise StageError(f"output {output} already exists; it is never reused or overwritten")

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.parent / f".{output.name}.stage-{uuid.uuid4().hex}"
    os.mkdir(temp)
    created_output = created_region = False
    moved: list[Path] = []
    written = 0
    success = False

    def reserve(size: int) -> None:
        nonlocal written
        if written + size > max_write_bytes:
            raise StageError("write budget exceeded")
        # Advisory check before every write, not a reservation against other writers.
        if shutil.disk_usage(temp).free < size + min_free_bytes:
            raise StageError("free disk reserve would be exhausted")
        written += size

    try:
        try:
            os.mkdir(output)  # exclusive reservation; COMPLETE inside it is what marks a staged tile
        except FileExistsError:
            raise StageError(f"output {output} already exists; it is never reused or overwritten") from None
        created_output = True
        start = _now()
        level = _copy_file(level_dat, temp / LEVEL_FILE, MAX_LEVEL_BYTES, LEVEL_FILE, reserve)
        files = {LEVEL_FILE: level["sha256"]}
        entries = []
        for rx, rz in regions:
            name = f"r.{rx}.{rz}.mca"
            source = cs.region_path(region_dir, rx, rz)
            if not os.path.lexists(source):
                entries.append({"x": rx, "z": rz, "state": "absent"})
                continue
            record = _copy_file(source, temp / "region" / name, MAX_REGION_BYTES, f"region/{name}", reserve)
            entries.append({"x": rx, "z": rz, "state": "present", "file": f"region/{name}", **record})
            files[f"region/{name}"] = record["sha256"]

        # Copied bytes are re-read, then every source is re-read and absent regions must still be absent.
        for relative, digest in files.items():
            if _hash_copy(temp / relative) != digest:
                raise StageError(f"staged {relative} does not match the bytes that were copied")
        if _hash_source(level_dat, MAX_LEVEL_BYTES, LEVEL_FILE, level['observedMetadata']) != (level["sha256"], level["size"]):
            raise StageError("level.dat changed during staging")
        for entry in entries:
            source = cs.region_path(region_dir, entry["x"], entry["z"])
            if entry["state"] == "absent":
                if os.path.lexists(source):
                    raise StageError(f"{source.name} appeared during staging")
            elif _hash_source(source, MAX_REGION_BYTES, entry["file"], entry['observedMetadata']) != (entry["sha256"], entry["size"]):
                raise StageError(f"{entry['file']} changed during staging")
        end = _now()

        provenance = {
            "format": FORMAT, "consistency": CONSISTENCY, "consistencyNote": CONSISTENCY_NOTE,
            "atomicWorldSnapshot": False,
            "tile": {"x": tile_x, "z": tile_z}, "input": _area(area), "core": _area(core),
            "sourceInterval": {"startUtc": start, "endUtc": end},
            "sources": {"levelDat": {"path": str(level_dat), **level}, "regionDir": str(region_dir)},
            "regions": entries, "files": files,
        }
        provenance_bytes = _json_bytes(provenance)
        complete_bytes = _json_bytes({"format": FORMAT, "provenanceSha256": hashlib.sha256(provenance_bytes).hexdigest()})
        reserve(len(provenance_bytes) + len(complete_bytes))
        _write_new(temp / PROVENANCE_FILE, provenance_bytes)
        _write_new(temp / COMPLETE_FILE, complete_bytes)

        # Move into the reserved output: data and provenance first, COMPLETE last.
        # Keep a valid empty region directory even when no region exists in this window.
        os.mkdir(output / "region")
        created_region = True
        for relative in (*files, PROVENANCE_FILE):
            if os.path.lexists(output / relative):
                raise StageError(f"unexpected {relative} in the reserved output")
            os.replace(temp / relative, output / relative)
            moved.append(output / relative)
        for relative, digest in files.items():
            if _hash_copy(output / relative) != digest:
                raise StageError(f"moved {relative} does not match the copied bytes")
        if created_region:
            _sync_dir(output / "region")
        _sync_dir(output)
        if os.path.lexists(output / COMPLETE_FILE):
            raise StageError("unexpected COMPLETE in the reserved output")
        os.replace(temp / COMPLETE_FILE, output / COMPLETE_FILE)
        moved.append(output / COMPLETE_FILE)
        _sync_dir(output)
        success = True
        return provenance
    finally:
        try:
            if not success:
                # Only what this run created: moved files, then the directories it made (if now empty).
                for path in reversed(moved):
                    path.unlink(missing_ok=True)
                if created_region:
                    try:
                        os.rmdir(output / "region")
                    except OSError:
                        pass
                if created_output:
                    try:
                        os.rmdir(output)
                    except OSError:
                        pass
        finally:
            if temp.exists() and not temp.is_symlink() and temp.name.startswith(f".{output.name}.stage-"):
                shutil.rmtree(temp)

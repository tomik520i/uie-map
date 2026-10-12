"""One-time import of the legacy static web export (web/data) as the INITIAL immutable generation.

import_legacy_generation(source, store, *, world, dimension, expected_manifest_sha256,
                         min_free_bytes=1 GiB, max_write_bytes=None) -> UIE_REVISION1 pointer

The legacy export is a UIE1-era static sample (manifest format 1, chunk_<x>_<z>.json meshes, optional
UIE_BLOCKS1 block files that may be partial at area boundaries). It is NOT a UIE2 batch and is never
labelled as one; publish_generation.py stays the only path for UIE2 captures and is not weakened here.

Contract:
- Initial store only: an existing current.json (or one appearing during the import) is refused; the
  pointer is created exclusively (hard link of an own temp file), never replaced.
- The exact source manifest.json bytes must hash to expected_manifest_sha256. Every referenced file is
  read through non-symlink components below the source root, validated, hashed, and re-read and
  re-hashed before promotion. Source files are only read.
- Mesh, block and atlas bytes are preserved exactly. Meshes and blocks are stored only as deterministic
  gzip (mtime 0, OS byte 255, round trip verified) under logical content-addressed names
  chunks/<x>_<z>-<sha256>.json and blocks/<x>_<z>-<sha256>.json (the hash of the uncompressed bytes).
  All manifest display fields are kept; only chunk/block file references and the atlas name are rebuilt.
- COMPLETE.json (UIE_GENERATION1) records importKind legacy-static-export, the source manifest SHA, the
  source file inventory, snapshots [] (no geometry capture provenance is asserted) and the block chunks
  whose data cover less than 16x16 columns. pipelineKey is the SHA-256 of the canonical source inventory:
  the identity of the imported bytes, NOT a modpack/pipeline assertion.
- One writer (.publish.lock, shared with publish_generation), a private stage, write budget and free-disk
  reserve checked for every write including the pointer (preflight before promotion), no overwrite of an
  existing generation (it must be byte-identical), and deletion only of this invocation's stage/temp file.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import shutil
import stat
import uuid
from datetime import datetime
from pathlib import Path

from publish_generation import PublicationError, canonical, digest, validate_mesh

IMPORT_KIND = "legacy-static-export"
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}")
SAFE_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
TOP_REQUIRED = {"format", "title", "sample", "generatedAt", "atlas", "chunks", "bounds", "coverage"}
TOP_OPTIONAL = {"bakedLighting", "focus", "blockInfo", "snapshotFormat", "snapshotSha256"}
COVERAGE_REQUIRED = {"renderedBlocks", "unsupportedBlocks", "unsupportedNames"}
COVERAGE_OPTIONAL = {"emptyBlocks"}
BLOCK_FIELDS = {"format", "x", "z", "width", "depth", "height", "registry", "runs"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class LegacyImportError(PublicationError):
    """The legacy export cannot be imported safely; nothing was published."""


def _int(value) -> bool:
    return type(value) is int


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _vec3(value) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_finite(v) for v in value)


def _sha(value) -> bool:
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def _redirected(info) -> bool:
    """A symlink, or on Windows any reparse point (junction, mount point, cloud placeholder)."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _refuse_redirection(path: Path, label: str) -> None:
    """path and every existing ancestor must be real entries. Missing parts are created later as real
    directories. Only lstat is used (no resolve/compare), so path casing never causes a refusal."""
    for entry in (path, *path.parents):
        try:
            info = os.lstat(entry)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise LegacyImportError(f"{label}: cannot inspect {entry} ({exc})") from None
        if _redirected(info):
            raise LegacyImportError(f"{label}: {entry} is a symlink or reparse point; pass the real path")


def _read(root: Path, name, limit: int = MAX_FILE_BYTES) -> bytes:
    """Bytes of root/name; every component below root must be a real (non-symlink, non-reparse) entry.
    lstat and open are separate steps: a concurrent swap between them is not defended against."""
    if not isinstance(name, str) or not SAFE_PATH.fullmatch(name) or any(p in (".", "..") for p in name.split("/")):
        raise LegacyImportError(f"unsafe source path {name!r}")
    path = root
    try:
        parts = name.split("/")
        for i, part in enumerate(parts):
            path = path / part
            info = os.lstat(path)
            if _redirected(info):
                raise LegacyImportError(f"{name}: symlinks and reparse points are refused")
            if i < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                raise LegacyImportError(f"{name}: a path component is not a directory")
        if not stat.S_ISREG(info.st_mode):
            raise LegacyImportError(f"{name}: not a regular file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
                raise LegacyImportError(f"{name}: not a regular file of at most {limit} bytes")
            # Read the actual size (+1 to detect growth): read(limit + 1) preallocates the whole limit on Windows.
            raw = stream.read(opened.st_size + 1)
            if len(raw) > opened.st_size:
                raw += stream.read(limit + 1 - len(raw))
    except OSError as exc:
        raise LegacyImportError(f"{name}: cannot read ({exc})") from None
    if len(raw) > limit:
        raise LegacyImportError(f"{name}: larger than {limit} bytes")
    return raw


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LegacyImportError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(name):
    raise LegacyImportError(f"non-finite JSON number {name}")


def _json(raw: bytes, where: str):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyImportError(f"{where}: not UTF-8 JSON ({exc})") from None


def _entries(values, label: str, keys: set) -> list:
    """[(x, z, file)] with unique coordinates and files."""
    if not isinstance(values, list) or not values:
        raise LegacyImportError(f"{label} must be a non-empty list")
    result, coords, files = [], set(), set()
    for i, entry in enumerate(values):
        if not isinstance(entry, dict) or set(entry) != keys or not _int(entry["x"]) or not _int(entry["z"]) \
                or not isinstance(entry["file"], str):
            raise LegacyImportError(f"{label}[{i}] must have exactly integer x/z and a file")
        key = (entry["x"], entry["z"])
        if key in coords or entry["file"] in files:
            raise LegacyImportError(f"{label}[{i}] repeats chunk {key} or file {entry['file']}")
        coords.add(key)
        files.add(entry["file"])
        result.append((entry["x"], entry["z"], entry["file"]))
    return result


def validate_legacy_manifest(m) -> tuple[list, dict | None, str | None]:
    """(geometry entries, block files by (x, z) or None, legacy block snapshot SHA or None)."""
    if not isinstance(m, dict):
        raise LegacyImportError("manifest root is not an object")
    missing, unknown = TOP_REQUIRED - set(m), set(m) - TOP_REQUIRED - TOP_OPTIONAL
    if missing or unknown:
        raise LegacyImportError(f"manifest fields: missing {sorted(missing)}, unknown {sorted(unknown)}")
    if not _int(m["format"]) or m["format"] != 1:
        raise LegacyImportError("manifest format must be 1")
    if m["sample"] is not True:
        raise LegacyImportError("the legacy export must be marked sample: true")
    if not isinstance(m["title"], str):
        raise LegacyImportError("title must be a string")
    try:
        when = datetime.fromisoformat(m["generatedAt"].replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise LegacyImportError("generatedAt is not an ISO 8601 timestamp") from None
    if when.tzinfo is None:
        raise LegacyImportError("generatedAt needs a timezone")
    if "bakedLighting" in m and type(m["bakedLighting"]) is not bool:
        raise LegacyImportError("bakedLighting must be a boolean")
    bounds = m["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != {"min", "max"} or not _vec3(bounds["min"]) \
            or not _vec3(bounds["max"]) or any(a > b for a, b in zip(bounds["min"], bounds["max"])):
        raise LegacyImportError("bounds must be finite {min, max} with min <= max")
    if "focus" in m and not _vec3(m["focus"]):
        raise LegacyImportError("focus must be three finite numbers")
    coverage = m["coverage"]
    if not isinstance(coverage, dict) or COVERAGE_REQUIRED - set(coverage) \
            or set(coverage) - COVERAGE_REQUIRED - COVERAGE_OPTIONAL:
        raise LegacyImportError("coverage has unexpected fields")
    for key in ("renderedBlocks", "unsupportedBlocks", "emptyBlocks"):
        if key in coverage and (not _int(coverage[key]) or coverage[key] < 0):
            raise LegacyImportError(f"coverage.{key} must be a non-negative integer")
    if not isinstance(coverage["unsupportedNames"], list) or not all(isinstance(n, str) for n in coverage["unsupportedNames"]):
        raise LegacyImportError("coverage.unsupportedNames must be strings")
    if "snapshotFormat" in m and m["snapshotFormat"] != "UIE1":
        raise LegacyImportError("only UIE1-era exports are imported; UIE2 batches use publish_generation")
    if "snapshotSha256" in m and not _sha(m["snapshotSha256"]):
        raise LegacyImportError("snapshotSha256 is not 64 lowercase hex")
    chunks = _entries(m["chunks"], "chunks", {"file", "x", "z"})
    if "blockInfo" not in m:
        return chunks, None, None
    info = m["blockInfo"]
    if not isinstance(info, dict) or set(info) - {"format", "snapshotSha256", "chunks"} \
            or info.get("format") != "UIE_BLOCKS1" or "chunks" not in info:
        raise LegacyImportError("blockInfo must be UIE_BLOCKS1 with chunks")
    snapshot = info.get("snapshotSha256")
    if snapshot is not None and not _sha(snapshot):
        raise LegacyImportError("blockInfo.snapshotSha256 is not 64 lowercase hex")
    blocks = {(x, z): file for x, z, file in _entries(info["chunks"], "blockInfo.chunks", {"file", "x", "z"})}
    geometry = {(x, z) for x, z, _ in chunks}
    if set(blocks) != geometry:
        raise LegacyImportError(f"block chunks differ from geometry chunks: missing {sorted(geometry - set(blocks))}, "
                                f"extra {sorted(set(blocks) - geometry)}")
    return chunks, blocks, snapshot


def validate_legacy_blocks(doc, cx: int, cz: int, where: str) -> bool:
    """UIE_BLOCKS1 for chunk (cx, cz), possibly cropped at the export boundary. Returns True when partial."""
    if not isinstance(doc, dict) or set(doc) != BLOCK_FIELDS or doc["format"] != "UIE_BLOCKS1":
        raise LegacyImportError(f"{where}: not a UIE_BLOCKS1 document with the expected fields")
    x, z, width, depth, height = (doc[k] for k in ("x", "z", "width", "depth", "height"))
    if not all(_int(v) for v in (x, z, width, depth, height)) or height != 256:
        raise LegacyImportError(f"{where}: x/z/width/depth must be integers and height 256")
    if not (1 <= width <= 16 and 1 <= depth <= 16):
        raise LegacyImportError(f"{where}: width/depth must be 1..16")
    if not (cx * 16 <= x and x + width <= cx * 16 + 16 and cz * 16 <= z and z + depth <= cz * 16 + 16):
        raise LegacyImportError(f"{where}: area {x}+{width}, {z}+{depth} is not inside chunk {cx},{cz}")
    registry, runs = doc["registry"], doc["runs"]
    if not isinstance(registry, dict) or not isinstance(runs, list):
        raise LegacyImportError(f"{where}: invalid registry or runs")
    for key, name in registry.items():
        if not re.fullmatch(r"0|[1-9][0-9]{0,4}", key) or int(key) > 65535 or not isinstance(name, str) or not name:
            raise LegacyImportError(f"{where}: invalid registry entry {key!r}")
    total, expected = 0, width * depth * 256
    for i, run in enumerate(runs):
        if not isinstance(run, list) or len(run) != 3 or not all(_int(v) for v in run):
            raise LegacyImportError(f"{where}: runs[{i}] is not [count, id, meta]")
        count, block_id, meta = run
        if count < 1 or not 0 <= block_id <= 65535 or str(block_id) not in registry or not 0 <= meta <= 15:
            raise LegacyImportError(f"{where}: runs[{i}] has an invalid count, id or meta")
        total += count
        if total > expected:
            raise LegacyImportError(f"{where}: runs exceed {expected} voxels")
    if total != expected:
        raise LegacyImportError(f"{where}: runs cover {total} of {expected} voxels")
    return width != 16 or depth != 16


def _write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
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


def _gzip(raw: bytes) -> bytes:
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    compressed = compressed[:9] + b"\xff" + compressed[10:]  # OS byte: platform independent
    if gzip.decompress(compressed) != raw:
        raise LegacyImportError("compression round trip failed")
    return compressed


def _verify_existing(target: Path, inventory_bytes: bytes, files: dict) -> None:
    """An existing generation with the same id is accepted only as a real directory whose COMPLETE.json and
    every inventoried file are byte-identical, read without following symlinks and bounded."""
    info = os.lstat(target)
    if _redirected(info) or not stat.S_ISDIR(info.st_mode):
        raise LegacyImportError(f"existing generation {target.name} is redirected or not a directory")
    try:
        same = _read(target, "COMPLETE.json") == inventory_bytes \
            and all(digest(_read(target, name)) == expected for name, expected in files.items())
    except LegacyImportError as exc:
        raise LegacyImportError(f"existing generation {target.name} cannot be verified safely: {exc}") from None
    if not same:
        raise LegacyImportError("an existing generation with this id differs; it is not overwritten")


def import_legacy_generation(source, store, *, world, dimension, expected_manifest_sha256,
                             min_free_bytes=1024 ** 3, max_write_bytes=None) -> dict:
    if not all(isinstance(s, str) and s.strip() for s in (world, dimension)):
        raise LegacyImportError("world and dimension are required")
    if not _sha(expected_manifest_sha256):
        raise LegacyImportError("expected_manifest_sha256 must be 64 lowercase hex")
    if not _int(min_free_bytes) or min_free_bytes < 0:
        raise LegacyImportError("invalid free disk reserve")
    if max_write_bytes is not None and (not _int(max_write_bytes) or max_write_bytes < 0):
        raise LegacyImportError("invalid write budget")

    # Root and every ancestor of source and store are checked before anything is created.
    source_root = Path(os.path.abspath(source))
    _refuse_redirection(source_root, "source")
    try:
        root_info = os.lstat(source_root)
    except OSError as exc:
        raise LegacyImportError(f"source is not readable ({exc})") from None
    if not stat.S_ISDIR(root_info.st_mode):
        raise LegacyImportError("source must be a directory")
    store = Path(os.path.abspath(store))
    _refuse_redirection(store, "store")
    # Checked before the store is created, so an overlapping store never adds anything to the source.
    source_real, store_real = source_root.resolve(), store.resolve()
    if store_real == source_real or store_real.is_relative_to(source_real) or source_real.is_relative_to(store_real):
        raise LegacyImportError("source and store overlap")
    store.mkdir(parents=True, exist_ok=True)
    store = store.resolve()
    current_path = store / "current.json"
    if os.path.lexists(current_path):
        raise LegacyImportError("store already has current.json; the legacy import is for an initial store only")

    lock = store / ".publish.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise LegacyImportError("publisher lock exists; recover only after proving its writer stopped") from None
    os.close(fd)
    stage = None
    written = 0

    def bounded_write(path: Path, raw: bytes) -> None:
        nonlocal written
        if max_write_bytes is not None and written + len(raw) > max_write_bytes:
            raise LegacyImportError("write budget exceeded")
        # Advisory check before every write, not a reservation against other writers.
        if shutil.disk_usage(store).free < len(raw) + min_free_bytes:
            raise LegacyImportError("free disk reserve would be exhausted")
        _write(path, raw)
        written += len(raw)

    try:
        if os.path.lexists(current_path):
            raise LegacyImportError("store already has current.json; the legacy import is for an initial store only")
        manifest_raw = _read(source_root, "manifest.json", MAX_MANIFEST_BYTES)
        manifest_sha = digest(manifest_raw)
        if manifest_sha != expected_manifest_sha256:
            raise LegacyImportError("source manifest.json does not match expected_manifest_sha256 (stale or changed)")
        manifest = _json(manifest_raw, "manifest.json")
        chunks, blocks, block_snapshot = validate_legacy_manifest(manifest)
        source_files = {"manifest.json": manifest_sha}

        def read_source(name: str) -> bytes:
            raw = _read(source_root, name)
            if source_files.setdefault(name, digest(raw)) != digest(raw):
                raise LegacyImportError(f"{name}: changed while it was read twice")
            return raw

        atlas = read_source(manifest["atlas"])
        if not atlas.startswith(PNG_SIGNATURE):
            raise LegacyImportError("atlas is not a PNG")

        stage = store / f".import-staging-{uuid.uuid4().hex}"
        stage.mkdir()
        files = {}

        def put(name: str, raw: bytes) -> None:
            if name in files:
                raise LegacyImportError(f"output collision at {name}")
            bounded_write(stage / name, raw)
            files[name] = digest(raw)

        put("atlas.png", atlas)
        chunk_refs, block_refs, partial = [], [], []
        for x, z, file in chunks:
            raw = read_source(file)
            try:
                validate_mesh(_json(raw, file))
            except LegacyImportError:
                raise
            except PublicationError as exc:
                raise LegacyImportError(f"{file}: {exc}") from None
            name = f"chunks/{x}_{z}-{digest(raw)}.json"
            put(name + ".gz", _gzip(raw))
            chunk_refs.append({"x": x, "z": z, "file": name})
            if blocks is not None:
                block_file = blocks[(x, z)]
                block_raw = read_source(block_file)
                if validate_legacy_blocks(_json(block_raw, block_file), x, z, block_file):
                    partial.append([x, z])
                block_name = f"blocks/{x}_{z}-{digest(block_raw)}.json"
                put(block_name + ".gz", _gzip(block_raw))
                block_refs.append({"x": x, "z": z, "file": block_name})

        # All display fields are kept as they are; only file references and the atlas name are rebuilt.
        output = {key: value for key, value in manifest.items() if key not in ("chunks", "blockInfo")}
        output["atlas"] = "atlas.png"
        output["chunks"] = chunk_refs
        if blocks is not None:
            output["blockInfo"] = {"format": "UIE_BLOCKS1", "chunks": block_refs}
            if block_snapshot is not None:
                output["blockInfo"]["snapshotSha256"] = block_snapshot
        put("manifest.json", canonical(output))

        pipeline_key = digest(canonical({"importKind": IMPORT_KIND, "sourceFiles": source_files}))
        identity = {"world": world, "dimension": dimension, "pipelineKey": pipeline_key}
        inventory = {"format": "UIE_GENERATION1", **identity, "pipelineIdentitySource": "legacy-export-inventory",
                     "importKind": IMPORT_KIND, "sourceManifestSha256": manifest_sha, "sourceFiles": source_files,
                     "snapshots": [], "partialBlockChunks": sorted(partial), "files": files}
        inventory_bytes = canonical(inventory)
        generation = digest(inventory_bytes)
        bounded_write(stage / "COMPLETE.json", inventory_bytes)
        for name, expected in files.items():
            if digest((stage / name).read_bytes()) != expected:
                raise LegacyImportError(f"staged {name} changed")
        for directory in sorted((p for p in stage.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            _sync_dir(directory)
        _sync_dir(stage)
        # The whole source inventory is re-read and re-hashed before anything becomes visible.
        for name, expected in source_files.items():
            limit = MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_FILE_BYTES
            if digest(_read(source_root, name, limit)) != expected:
                raise LegacyImportError(f"source file {name} changed during the import")

        pointer = {"format": "UIE_REVISION1", **identity, "generation": generation,
                   "manifest": f"generations/{generation}/manifest.json", "manifestSha256": files["manifest.json"]}
        pointer_raw = canonical(pointer)
        # Predictable pointer failures abort here, while only the private stage exists.
        if max_write_bytes is not None and written + len(pointer_raw) > max_write_bytes:
            raise LegacyImportError("write budget exceeded")
        if shutil.disk_usage(store).free < len(pointer_raw) + min_free_bytes:
            raise LegacyImportError("free disk reserve would be exhausted")
        if os.path.lexists(current_path):
            raise LegacyImportError("current.json appeared during the import; nothing was promoted")

        parent = store / "generations"
        if os.path.lexists(parent) and _redirected(os.lstat(parent)):
            raise LegacyImportError("generations directory is a symlink or reparse point")
        parent.mkdir(exist_ok=True)
        target = parent / generation
        if os.path.lexists(target):
            _verify_existing(target, inventory_bytes, files)
        else:
            os.rename(stage, target)
        _sync_dir(parent)

        temp = store / f".current-{uuid.uuid4().hex}.tmp"
        try:
            bounded_write(temp, pointer_raw)
        except FileExistsError:
            raise  # never remove a temp file this invocation did not create
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        try:
            # Exclusive creation: a pointer that appeared concurrently is never replaced.
            os.link(temp, current_path)
        except FileExistsError:
            raise LegacyImportError("current.json appeared concurrently; the imported generation stays unpublished") from None
        finally:
            temp.unlink(missing_ok=True)
        _sync_dir(store)
        return pointer
    finally:
        try:
            if stage is not None and stage.exists():
                # Only this invocation's own stage directory is ever removed.
                if stage.is_symlink() or stage.resolve().parent != store or not stage.name.startswith(".import-staging-"):
                    raise LegacyImportError("refusing cleanup of a redirected staging directory")
                shutil.rmtree(stage)
        finally:
            lock.unlink(missing_ok=True)

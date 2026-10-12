"""UIE_BLOCKS1 block identification for one UIE2 batch (core chunks only), for batch publication.

The web contract is unchanged (web/block-info.js): one file per chunk
    {"format": "UIE_BLOCKS1", "x": cx*16, "z": cz*16, "width": 16, "depth": 16, "height": 256,
     "registry": {"<id>": "<name>", ...}, "runs": [[count, id, meta], ...]}
voxel order ((z - z0) * 16 + (x - x0)) * 256 + y, one registry per chunk (ids used in that chunk only),
written to <data_dir>/blocks/<sha256 of the snapshot bytes>/chunk_<cx>_<cz>.json.

Only chunks that are inside the UIE2 core AND present in its mask are exported. Halo chunks are never
exported (they belong to neighbouring batches) and absent core chunks are omitted, never written as air.

export_batch_block_info(snapshot_path, data_dir) -> blockInfo
    {"format": "UIE_BLOCKS1", "snapshotSha256", "chunks": [{"x", "z", "file"}]}  (chunks sorted by z, x)
    Files are immutable: an existing file must be byte-identical, otherwise BatchBlockInfoError.
attach_batch_block_info(snapshot_path, manifest_path) -> blockInfo
    Requires manifest format 1, snapshotFormat "UIE2", snapshotSha256 equal to the digest of the snapshot
    bytes that are parsed, unique chunk coordinates/files and a chunk set exactly equal to the present core
    chunks. All checks happen before any file is written. The snapshot and manifest are re-read before the
    manifest is replaced (temp file + fsync + os.replace); an existing different blockInfo is not overwritten.
    On failure the manifest is untouched; already written block files stay unreferenced.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import capture_batch as cb
import capture_snapshot as cs

FORMAT = "UIE_BLOCKS1"
CHUNK = 16
HEIGHT = 256


class BatchBlockInfoError(cs.SnapshotError):
    """Block identification cannot be exported or attached safely."""


def _load(snapshot_path) -> tuple[Path, bytes, str, cb.BatchSnapshot]:
    path = Path(snapshot_path).resolve()
    raw = path.read_bytes()
    return path, raw, hashlib.sha256(raw).hexdigest(), cb.parse_batch(raw)


def core_present_chunks(batch: cb.BatchSnapshot) -> list[tuple[int, int]]:
    """Present chunks inside the core, sorted by (z, x)."""
    core = batch.core
    chunks = [(cx, cz) for cz in range(core.z >> 4, (core.z + core.depth) >> 4)
              for cx in range(core.x >> 4, (core.x + core.width) >> 4)]
    return [c for c in chunks if c in batch.present_chunks]


def chunk_document(batch: cb.BatchSnapshot, cx: int, cz: int) -> dict:
    if (cx, cz) not in core_present_chunks(batch):
        raise BatchBlockInfoError(f"chunk ({cx},{cz}) is not a present core chunk")
    x0, z0 = cx * CHUNK, cz * CHUNK
    runs, registry = [], {}
    for z in range(z0, z0 + CHUNK):
        for x in range(x0, x0 + CHUNK):
            _, column = batch.column(x, z)
            for block_id, meta in column:
                name = batch.registry.get(block_id)
                if not name or not 0 <= meta <= 15:
                    raise BatchBlockInfoError(f"invalid block {block_id}:{meta} at ({x},{z})")
                registry[str(block_id)] = name
                if runs and runs[-1][1] == block_id and runs[-1][2] == meta:
                    runs[-1][0] += 1
                else:
                    runs.append([1, block_id, meta])
    return {"format": FORMAT, "x": x0, "z": z0, "width": CHUNK, "depth": CHUNK, "height": HEIGHT,
            "registry": dict(sorted(registry.items(), key=lambda item: int(item[0]))), "runs": runs}


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.is_symlink() or path.resolve() != path:
        raise BatchBlockInfoError(f"block output path is redirected: {path}")
    if path.exists():
        if path.read_bytes() != payload:
            raise BatchBlockInfoError(f"unexpected content in immutable block file {path}")
        return
    # Exclusive creation: an interrupted earlier write is diagnosed later as a mismatch, never trusted.
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _export(path: Path, raw: bytes, digest: str, batch: cb.BatchSnapshot, data_dir) -> dict:
    data_dir = Path(data_dir).resolve()
    target = data_dir / "blocks" / digest
    if path == target or target in path.parents:
        raise BatchBlockInfoError("block output directory would contain the snapshot")
    real = target.resolve()
    if real != target or data_dir not in real.parents:
        raise BatchBlockInfoError(f"block output {target} resolves outside the data directory")
    target.mkdir(parents=True, exist_ok=True)
    chunks = []
    for cx, cz in core_present_chunks(batch):
        payload = (json.dumps(chunk_document(batch, cx, cz), ensure_ascii=False, separators=(",", ":"))
                   + "\n").encode("utf-8")
        file = target / f"chunk_{cx}_{cz}.json"
        _write_immutable(file, payload)
        chunks.append({"x": cx, "z": cz, "file": file.relative_to(data_dir).as_posix()})
    if path.read_bytes() != raw:
        raise BatchBlockInfoError("snapshot changed during block export")
    return {"format": FORMAT, "snapshotSha256": digest, "chunks": chunks}


def export_batch_block_info(snapshot_path, data_dir) -> dict:
    path, raw, digest, batch = _load(snapshot_path)
    return _export(path, raw, digest, batch, data_dir)


def _manifest_chunks(manifest: dict) -> set[tuple[int, int]]:
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list):
        raise BatchBlockInfoError("manifest chunks is not a list")
    coords, files = set(), set()
    for i, chunk in enumerate(chunks):
        if (not isinstance(chunk, dict) or not isinstance(chunk.get("file"), str)
                or not all(isinstance(chunk.get(k), int) and not isinstance(chunk.get(k), bool) for k in ("x", "z"))):
            raise BatchBlockInfoError(f"manifest chunks[{i}] needs integer x/z and a file")
        key = (chunk["x"], chunk["z"])
        if key in coords or chunk["file"] in files:
            raise BatchBlockInfoError(f"manifest lists chunk {key} or file {chunk['file']} twice")
        coords.add(key)
        files.add(chunk["file"])
    return coords


def attach_batch_block_info(snapshot_path, manifest_path) -> dict:
    path, raw, digest, batch = _load(snapshot_path)
    manifest_path = Path(manifest_path).resolve()
    if manifest_path == path:
        raise BatchBlockInfoError("manifest and snapshot are the same file")
    manifest_raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchBlockInfoError(f"manifest is not JSON: {exc}") from None
    if not isinstance(manifest, dict) or type(manifest.get("format")) is not int or manifest.get("format") != 1:
        raise BatchBlockInfoError("manifest is not a format 1 geometry manifest")
    if manifest.get("snapshotFormat") != "UIE2":
        raise BatchBlockInfoError(f"manifest snapshotFormat is {manifest.get('snapshotFormat')!r}, not 'UIE2'")
    if manifest.get("snapshotSha256") != digest:
        raise BatchBlockInfoError("manifest snapshotSha256 does not match the snapshot bytes")
    expected = set(core_present_chunks(batch))
    actual = _manifest_chunks(manifest)
    if actual != expected:
        raise BatchBlockInfoError(f"manifest chunk set differs from the present core chunks: "
                                  f"missing {sorted(expected - actual)}, extra {sorted(actual - expected)}")
    planned = {"format": FORMAT, "snapshotSha256": digest,
               "chunks": [{"x": cx, "z": cz, "file": f"blocks/{digest}/chunk_{cx}_{cz}.json"}
                          for cx, cz in core_present_chunks(batch)]}
    if "blockInfo" in manifest and manifest["blockInfo"] != planned:
        raise BatchBlockInfoError("manifest already has a different blockInfo; refusing to overwrite it")
    info = _export(path, raw, digest, batch, manifest_path.parent)
    if info != planned:
        raise BatchBlockInfoError("exported blockInfo differs from the planned one")
    if path.read_bytes() != raw or manifest_path.read_bytes() != manifest_raw:
        raise BatchBlockInfoError("snapshot or manifest changed during block export")
    manifest["blockInfo"] = info
    fd, temp = tempfile.mkstemp(dir=manifest_path.parent, prefix=f".{manifest_path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, manifest_path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    if hasattr(os, "O_DIRECTORY"):  # make the rename durable where the platform allows it
        dir_fd = os.open(manifest_path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    return info

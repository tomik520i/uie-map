"""Optional LOD block for publish_generation (Python side of web/lod-contract.js, UIE_LOD1 / planar-unit-v1).

lod_directory layout:
    lod.json                       exactly the manifest.lod block: {format, method, colorSteps, atlasSha256, chunks}
    lod/<x>_<z>-<sha256>.json      UIE_LOD_CHUNK1 coarse documents named by the SHA-256 of their bytes

publish_lod(read_source, detail, atlas_sha, put) validates lod.json against the REAL published detail
geometry (detail[(x, z)] = (sha256 of the detail payload, [(vertices, indices), ...] of [main, ...transparentParts]))
and the real atlas hash, then streams each coarse file once: its bytes must hash to the name before they are
decoded, the document is validated, and put() stores it. Returns the lod block for the manifest.

The rules follow lod-contract.js, with Python being stricter where JSON numbers are concerned: counts and
coordinates must be JSON integers (a JSON 3.0 is refused; JS cannot tell 3.0 from 3), duplicate JSON keys and
NaN/Infinity are refused.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

from publish_generation import PublicationError

LOD_FORMAT = "UIE_LOD1"
LOD_METHOD = "planar-unit-v1"
LOD_CHUNK_FORMAT = "UIE_LOD_CHUNK1"
COLOR_STEPS = 8
MAX_SAFE = 2 ** 53 - 1
HEX64 = re.compile(r"[0-9a-f]{64}")
INT = r"(0|-?[1-9][0-9]*)"
LOD_FILE = re.compile(rf"lod/{INT}_{INT}-([0-9a-f]{{64}})\.json")
PART_KEYS = {"positions", "uvs", "colors", "indices", "transparent"}


class LodPublicationError(PublicationError):
    """The LOD block or a coarse document does not satisfy the frozen contract."""


def _safe_int(v) -> bool:
    return type(v) is int and -MAX_SAFE <= v <= MAX_SAFE


def _count(v) -> bool:
    return _safe_int(v) and v >= 0


def _number(v) -> bool:
    return type(v) in (int, float) and math.isfinite(v) and (type(v) is float or abs(v) <= MAX_SAFE)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LodPublicationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _constant(name):
    raise LodPublicationError(f"non-finite JSON number {name}")


def _json(raw: bytes, where: str):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LodPublicationError(f"{where}: not UTF-8 JSON ({exc})") from None


def _keys(obj, keys, where):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise LodPublicationError(f"{where} must be exactly {{{', '.join(keys)}}}")


def count_parts(parts, where, minimum=0) -> list:
    if not isinstance(parts, list) or len(parts) < minimum:
        raise LodPublicationError(f"{where} must be a list of at least {minimum} parts")
    result = []
    for i, part in enumerate(parts):
        _keys(part, ("vertices", "indices"), f"{where}[{i}]")
        v, n = part["vertices"], part["indices"]
        if not _count(v) or not _count(n) or n % 3 or (v == 0 and n > 0):
            raise LodPublicationError(f"{where}[{i}]: counts must be safe integers >= 0, indices a multiple of 3")
        result.append((v, n))
    return result


def mesh_bytes(counts) -> int:
    """Same as lod-contract.js meshBytesFromCounts / app.js chunkBufferBytes."""
    total = 0
    for v, n in counts:
        if n:
            total += 2 * (v * 44 + n * (4 if v > 65535 else 2))
            if total > MAX_SAFE:
                raise LodPublicationError("mesh bytes exceed the safe integer range")
    return total


def detail_counts(mesh: dict) -> list:
    """[(vertices, indices)] of [main, ...transparentParts] of an already validated detail mesh."""
    return [(len(p["positions"]) // 3, len(p["indices"])) for p in [mesh, *mesh.get("transparentParts", [])]]


def _part(part, where, role):
    if not isinstance(part, dict) or not set(part) <= PART_KEYS:
        raise LodPublicationError(f"{where} may only contain positions, uvs, colors, indices and transparent")
    arrays = [part.get(k) for k in ("positions", "uvs", "colors", "indices")]
    if not all(isinstance(a, list) for a in arrays):
        raise LodPublicationError(f"{where} needs positions/uvs/colors/indices arrays")
    positions, uvs, colors, indices = arrays
    if "transparent" in part:
        t = part["transparent"]
        ok = type(t) is bool if role == "textured" else (t is False if role == "untextured" else t is True)
        if not ok:
            raise LodPublicationError(f"{where}.transparent is not allowed here")
    if len(positions) % 3:
        raise LodPublicationError(f"{where}.positions length is not a multiple of 3")
    n = len(positions) // 3
    if len(uvs) != 2 * n or len(colors) != 3 * n or len(indices) % 3:
        raise LodPublicationError(f"{where}: array lengths do not match {n} vertices")
    if not all(_number(v) for arr in (positions, uvs, colors) for v in arr):
        raise LodPublicationError(f"{where}: positions/uvs/colors must be finite numbers")
    if not all(_safe_int(i) and 0 <= i < n for i in indices):
        raise LodPublicationError(f"{where}: index outside 0..{n - 1}")
    return n, len(indices)


def validate_coarse(doc, entry, coarse_counts, atlas_sha):
    file = entry["file"]
    _keys(doc, ("format", "x", "z", "sourceSha256", "atlasSha256", "colorSteps", "untextured", "textured",
                "transparentParts"), file)
    if doc["format"] != LOD_CHUNK_FORMAT:
        raise LodPublicationError(f"{file}: format must be {LOD_CHUNK_FORMAT}")
    expected = {"x": entry["x"], "z": entry["z"], "sourceSha256": entry["sourceSha256"], "atlasSha256": atlas_sha,
                "colorSteps": COLOR_STEPS}
    for key, value in expected.items():
        if doc[key] != value or type(doc[key]) is not type(value):
            raise LodPublicationError(f"{file}: {key} does not match the manifest")
    if not isinstance(doc["transparentParts"], list):
        raise LodPublicationError(f"{file}: transparentParts must be a list")
    counts = [_part(doc["untextured"], f"{file} untextured", "untextured"),
              _part(doc["textured"], f"{file} textured", "textured"),
              *(_part(p, f"{file} transparentParts[{i}]", "transparent") for i, p in enumerate(doc["transparentParts"]))]
    if counts != coarse_counts:
        raise LodPublicationError(f"{file}: part counts do not match lod.json")


def publish_lod(read_source, detail: dict, atlas_sha: str, put) -> dict:
    lod = _json(read_source("lod.json"), "lod.json")
    _keys(lod, ("format", "method", "colorSteps", "atlasSha256", "chunks"), "lod.json")
    if lod["format"] != LOD_FORMAT or lod["method"] != LOD_METHOD or lod["colorSteps"] != COLOR_STEPS \
            or type(lod["colorSteps"]) is not int:
        raise LodPublicationError(f"lod.json must be {LOD_FORMAT} / {LOD_METHOD} with colorSteps {COLOR_STEPS}")
    if not isinstance(lod["atlasSha256"], str) or not HEX64.fullmatch(lod["atlasSha256"]) or lod["atlasSha256"] != atlas_sha:
        raise LodPublicationError("lod.json atlasSha256 must equal the published atlas")
    if not isinstance(lod["chunks"], list):
        raise LodPublicationError("lod.json chunks must be a list")
    seen = set()
    for i, entry in enumerate(lod["chunks"]):
        where = f"lod.json chunks[{i}]"
        _keys(entry, ("x", "z", "file", "sourceSha256", "coarse", "detail"), where)
        x, z = entry["x"], entry["z"]
        if not _safe_int(x) or not _safe_int(z):
            raise LodPublicationError(f"{where} needs safe integer x/z")
        if (x, z) not in detail:
            raise LodPublicationError(f"{where}: {x},{z} is not a published detail chunk")
        if (x, z) in seen:
            raise LodPublicationError(f"{where} repeats {x},{z}")
        seen.add((x, z))
        match = LOD_FILE.fullmatch(entry["file"]) if isinstance(entry["file"], str) else None
        if not match or int(match.group(1)) != x or int(match.group(2)) != z:
            raise LodPublicationError(f"{where}.file must be lod/{x}_{z}-<sha256>.json")
        source_sha, source_counts = detail[(x, z)]
        if entry["sourceSha256"] != source_sha:
            raise LodPublicationError(f"{where}.sourceSha256 does not match the published detail chunk")
        for name in ("coarse", "detail"):
            _keys(entry[name], ("parts",), f"{where}.{name}")
        coarse_counts = count_parts(entry["coarse"]["parts"], f"{where}.coarse.parts", 2)
        if count_parts(entry["detail"]["parts"], f"{where}.detail.parts", 1) != source_counts:
            raise LodPublicationError(f"{where}.detail counts do not match the published detail geometry")
        mesh_bytes(coarse_counts), mesh_bytes(source_counts)
        # One coarse payload at a time: hash of the exact bytes first, then decode and validate, then store.
        raw = read_source(entry["file"])
        if hashlib.sha256(raw).hexdigest() != match.group(3):
            raise LodPublicationError(f"{entry['file']}: bytes do not match the hash in the name")
        validate_coarse(_json(raw, entry["file"]), entry, coarse_counts, atlas_sha)
        put(entry["file"], raw)
        del raw
    if seen != set(detail):
        missing = sorted(set(detail) - seen)[:5]
        raise LodPublicationError(f"lod.json must cover every detail chunk; missing {missing}")
    return lod

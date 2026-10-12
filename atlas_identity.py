"""Conservative offline comparison of two exported atlases (UIE_ATLAS_LAYOUT1) for future canonical reuse.

compare_atlases(atlas_a, layout_a, atlas_b, layout_b, *, pipeline_key_a, pipeline_key_b) -> dict
    {"compatible": bool, "reason": str, "layoutIdentitySha256": str | None, "atlasBytesEqual": bool}

Invalid input raises ValueError: an unreadable, symlinked, non-regular or oversized file, a PNG whose SHA-256 or
IHDR dimensions do not match its layout, or a layout that is not exactly the schema written by
client-exporter AtlasLayout.java plus the atlasSha256 added by AssetExport. Unknown or duplicate fields are
rejected, never ignored. Internal consistency is rechecked: placed extents (rotation-aware), stored UV ranges,
getterUvMatches against stored/getter UV bits, and reuseBlockers/reusable against the descriptors.

A valid pair is compatible only when both pipeline keys are equal, both layouts are reusable and the full layouts
are equal after removing atlasSha256 (canonical JSON, sorted keys, no NaN). layoutIdentitySha256 is the SHA-256 of
that canonical layout and is returned only when compatible.

Limits of what this proves:
- Pipeline keys are caller assertions (the same mods/config/resources); nothing here verifies them.
- Layout equality proves only equal sprite names, placement and UVs. It never proves pixel equivalence. When the
  PNG bytes differ, the pair is merely a static-atlas candidate: animation frames, a different resource or any
  other pixel difference may remain. atlasBytesEqual reports only equality of the two SHA-256 digests.
- Nothing is copied, replaced or published.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
import zlib

FORMAT = "UIE_ATLAS_LAYOUT1"
MAX_LAYOUT_BYTES = 32 * 1024 * 1024
MAX_ATLAS_BYTES = 256 * 1024 * 1024
MAX_PIXELS = 67108864  # same cap as AssetExport/AtlasLayout
MAX_ENTRIES = 200000   # per map, as AtlasLayout.MAX_ENTRIES
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
HEX64 = re.compile(r"[0-9a-f]{64}")
TOP_FIELDS = {"format", "width", "height", "reusable", "reuseBlockers", "missing", "registered", "uploaded",
              "atlasSha256"}
SPRITE_FIELDS = {"iconName", "state", "x", "y", "width", "height", "rotated", "uv", "getterUv", "getterUvMatches"}
GETTER_MISMATCH = ": getter UV differs from stored UV"
UNSTITCHED = ": unstitched"


def _read(path, limit: int, what: str) -> bytes:
    """Bytes of one regular, non-symlink file of at most limit bytes."""
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{what} is not a regular file (symlinks are refused)")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"{what} is not a regular file")
            # POSIX: O_NOFOLLOW refuses a final symlink and st_dev/st_ino detect a swap. Windows stat and fstat
            # may report different device numbers, so there only the lstat check above applies.
            if os.name == "posix" and (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError(f"{what} changed while it was opened")
            if opened.st_size > limit:
                raise ValueError(f"{what} is larger than {limit} bytes")
            # Read the actual size (+1 to detect growth): read(limit + 1) preallocates the whole limit on Windows.
            raw = stream.read(opened.st_size + 1)
            if len(raw) > opened.st_size:
                raw += stream.read(limit + 1 - len(raw))
    except OSError as exc:
        raise ValueError(f"cannot read {what}: {exc}") from None
    if len(raw) > limit:
        raise ValueError(f"{what} is larger than {limit} bytes")
    return raw


def _png_size(raw: bytes, what: str) -> tuple[int, int]:
    """IHDR width/height with its CRC checked; the image data is not decoded."""
    if len(raw) < 33 or raw[:8] != PNG_SIGNATURE or raw[8:16] != b"\x00\x00\x00\x0dIHDR":
        raise ValueError(f"{what} is not a PNG with a leading IHDR chunk")
    if struct.unpack(">I", raw[29:33])[0] != zlib.crc32(raw[12:29]) & 0xFFFFFFFF:
        raise ValueError(f"{what} IHDR CRC mismatch")
    width, height = struct.unpack(">II", raw[16:24])
    if not (1 <= width <= 0x7FFFFFFF and 1 <= height <= 0x7FFFFFFF) or width * height > MAX_PIXELS:
        raise ValueError(f"{what} has invalid dimensions {width}x{height}")
    return width, height


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError(f"non-finite JSON number {name}")


def _integer(value) -> bool:
    return type(value) is int


def _float(value) -> bool:
    """The exporter writes every UV with Float.toString, which always yields a JSON float, never an integer."""
    return type(value) is float and math.isfinite(value)


def _same_bits(a: float, b: float) -> bool:
    """Java Float.floatToIntBits equality for values written by Float.toString (keeps -0.0 != 0.0)."""
    return struct.pack(">d", a) == struct.pack(">d", b)


def _utf16_order(text: str) -> bytes:
    """Java String.compareTo order (UTF-16 code units), as used by the exporter's TreeSet."""
    return text.encode("utf-16-be", "surrogatepass")


def _sprite(doc, label: str, width: int, height: int, may_be_unstitched: bool, blockers: set) -> dict:
    """Validates one descriptor and adds the reuse blockers it implies."""
    if not isinstance(doc, dict) or set(doc) != SPRITE_FIELDS:
        raise ValueError(f"{label}: descriptor fields must be exactly {sorted(SPRITE_FIELDS)}")
    if not isinstance(doc["iconName"], str) or not doc["iconName"]:
        raise ValueError(f"{label}: invalid iconName")
    x, y, w, h = (doc[k] for k in ("x", "y", "width", "height"))
    if not all(_integer(v) for v in (x, y, w, h)) or type(doc["rotated"]) is not bool:
        raise ValueError(f"{label}: x/y/width/height must be integers and rotated a boolean")
    uv, getter = doc["uv"], doc["getterUv"]
    if not isinstance(uv, list) or len(uv) != 4 or not all(_float(v) for v in uv):
        raise ValueError(f"{label}: uv must be four finite floats")
    if not isinstance(getter, list) or len(getter) != 4 or not all(v is None or _float(v) for v in getter):
        raise ValueError(f"{label}: getterUv must be four finite floats or null")
    matches = all(g is not None and _same_bits(s, g) for s, g in zip(uv, getter))
    if doc["getterUvMatches"] is not matches:
        raise ValueError(f"{label}: getterUvMatches contradicts uv/getterUv")
    if doc["state"] == "unstitched":
        if not may_be_unstitched or w != 0 or h != 0:
            raise ValueError(f"{label}: only a registered-only zero-size sprite may be unstitched")
        blockers.add(label + UNSTITCHED)
    elif doc["state"] == "placed":
        extent_x, extent_y = (h, w) if doc["rotated"] else (w, h)
        if x < 0 or y < 0 or w < 1 or h < 1 or x + extent_x > width or y + extent_y > height:
            raise ValueError(f"{label}: sprite outside the atlas")
        if not all(0 <= v <= 1 for v in uv) or uv[0] >= uv[2] or uv[1] >= uv[3]:
            raise ValueError(f"{label}: stored uv outside [0,1] or empty/reversed")
    else:
        raise ValueError(f"{label}: unknown state {doc['state']!r}")
    if not matches:
        blockers.add(label + GETTER_MISMATCH)
    return doc


def _sprites(doc, label: str, width: int, height: int, may_be_unstitched: bool, blockers: set) -> dict:
    # The exporter allows "unstitched" only for registered sprites absent from the uploaded map. That absence
    # is by object identity and is not recorded, so it cannot be rechecked here beyond the map itself.
    if not isinstance(doc, dict) or len(doc) > MAX_ENTRIES:
        raise ValueError(f"{label} must be an object with at most {MAX_ENTRIES} entries")
    for key, value in doc.items():
        if not key:
            raise ValueError(f"{label} has an empty key")
        _sprite(value, f"{label}/{key}", width, height, may_be_unstitched, blockers)
    return doc


def load_layout(atlas_path, layout_path) -> tuple[dict, str]:
    """(validated layout without atlasSha256, atlas SHA-256). Raises ValueError for any invalid input."""
    atlas = _read(atlas_path, MAX_ATLAS_BYTES, "atlas PNG")
    raw = _read(layout_path, MAX_LAYOUT_BYTES, "atlas layout")
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except UnicodeDecodeError as exc:
        raise ValueError(f"atlas layout is not UTF-8: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"atlas layout is not JSON: {exc}") from None
    if not isinstance(doc, dict) or set(doc) != TOP_FIELDS:
        raise ValueError(f"atlas layout fields must be exactly {sorted(TOP_FIELDS)}")
    if doc["format"] != FORMAT:
        raise ValueError(f"unsupported atlas layout format {doc['format']!r}")
    digest = hashlib.sha256(atlas).hexdigest()
    if not isinstance(doc["atlasSha256"], str) or not HEX64.fullmatch(doc["atlasSha256"]):
        raise ValueError("atlasSha256 must be 64 lowercase hex characters")
    if doc["atlasSha256"] != digest:
        raise ValueError("atlas PNG bytes do not match the layout atlasSha256")
    width, height = doc["width"], doc["height"]
    if not _integer(width) or not _integer(height) or width < 1 or height < 1:
        raise ValueError("layout width/height must be positive integers")
    if _png_size(atlas, "atlas PNG") != (width, height):
        raise ValueError("atlas PNG dimensions differ from the layout")
    if type(doc["reusable"]) is not bool:
        raise ValueError("reusable must be a boolean")
    listed = doc["reuseBlockers"]
    if not isinstance(listed, list) or not all(isinstance(b, str) for b in listed):
        raise ValueError("reuseBlockers must be a list of strings")

    blockers: set = set()
    _sprite(doc["missing"], "missing", width, height, False, blockers)
    if not _sprites(doc["uploaded"], "uploaded", width, height, False, blockers):
        raise ValueError("uploaded map is empty")
    _sprites(doc["registered"], "registered", width, height, True, blockers)
    if listed != sorted(blockers, key=_utf16_order):
        raise ValueError("reuseBlockers do not match the descriptors")
    if doc["reusable"] is not (not blockers):
        raise ValueError("reusable contradicts reuseBlockers")
    layout = dict(doc)
    del layout["atlasSha256"]
    return layout, digest


def layout_identity(layout: dict) -> str:
    canonical = json.dumps(layout, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _pipeline_key(value, name: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise ValueError(f"{name} must be 64 lowercase hex characters")
    return value


def compare_atlases(atlas_a, layout_a, atlas_b, layout_b, *, pipeline_key_a, pipeline_key_b) -> dict:
    key_a = _pipeline_key(pipeline_key_a, "pipeline_key_a")
    key_b = _pipeline_key(pipeline_key_b, "pipeline_key_b")
    doc_a, sha_a = load_layout(atlas_a, layout_a)
    doc_b, sha_b = load_layout(atlas_b, layout_b)
    same_bytes = sha_a == sha_b

    def result(compatible: bool, reason: str, identity=None) -> dict:
        return {"compatible": compatible, "reason": reason, "layoutIdentitySha256": identity,
                "atlasBytesEqual": same_bytes}

    if key_a != key_b:
        return result(False, "pipeline keys differ (caller assertions)")
    for name, doc in (("A", doc_a), ("B", doc_b)):
        if not doc["reusable"]:
            return result(False, f"layout {name} is not reusable: {len(doc['reuseBlockers'])} blocker(s)")
    identity_a, identity_b = layout_identity(doc_a), layout_identity(doc_b)
    if identity_a != identity_b:
        return result(False, "layouts differ")
    if same_bytes:
        return result(True, "identical atlas bytes and equal layout under the asserted pipeline key", identity_a)
    return result(True, "equal layout, different atlas bytes: static-atlas candidate only; "
                        "pixel equivalence is NOT established", identity_a)

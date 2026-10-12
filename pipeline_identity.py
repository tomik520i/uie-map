"""Deterministic identity of DECLARED renderer inputs (UIE_PIPELINE_IDENTITY1).

create_pipeline_identity(roots, files, options, *, max_files=20000, max_total_bytes=8 GiB) -> dict
    roots:   {label: directory}   every regular file below it, recursively (hidden files included)
    files:   {label: file}        single files, e.g. an exporter JAR or a JVM options file
    options: plain JSON object    caller-declared settings (no secrets, no environment dumps)
-> {"format": "UIE_PIPELINE_IDENTITY1", "pipelineKey": sha256 of the canonical identity,
    "identity": {"format": "UIE_RENDER_INPUTS1", "roots": [labels, including empty roots],
                 "files": [{"name": "<root label>/<posix path>" or "<file label>", "bytes", "sha256"}],
                 "options": {...}}}

The key depends only on labels, relative POSIX paths, sizes, content hashes and options: not on absolute
paths, mtimes or enumeration order. Contents are hashed in 1 MiB blocks.

This hashes what the caller declares. It does not discover dependencies: the caller must list every mod,
config, asset pack, library, native library, exporter build, Java runtime and option that affects
rendering. The OS, GPU driver, the world's block registry and the runtime-generated atlas are outside this
identity and must be bound separately. It is not an atomic snapshot: files are read twice (two full passes,
with per-file metadata checks) and any observed change is rejected, but the inputs must be immutable or
cooperatively frozen while the identity is computed.

Read-only: no output files, no processes, no network.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path

FORMAT = "UIE_PIPELINE_IDENTITY1"
INPUTS_FORMAT = "UIE_RENDER_INPUTS1"
LABEL = re.compile(r"[a-z][a-z0-9_-]{0,63}")
BLOCK = 1 << 20
MAX_SAFE = 2 ** 53 - 1
MAX_DEPTH = 64


class PipelineIdentityError(ValueError):
    """The declared inputs cannot be identified safely."""


def _after_first_pass() -> None:
    """Test hook between the two passes; does nothing in normal use."""


def _after_preflight() -> None:
    """Test hook after a pass's size preflight, before any file is read; does nothing in normal use."""


# ---------------------------------------------------------------- options

def _check_option(value, where: str, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise PipelineIdentityError(f"options nested deeper than {MAX_DEPTH} at {where}")
    kind = type(value)
    if value is None or kind is bool or kind is str:
        return
    if kind is int:
        if abs(value) > MAX_SAFE:
            raise PipelineIdentityError(f"{where}: integer outside the safe JSON range")
        return
    if kind is float:
        if not math.isfinite(value):
            raise PipelineIdentityError(f"{where}: non-finite number")
        return
    if kind is list:
        for i, item in enumerate(value):
            _check_option(item, f"{where}[{i}]", depth + 1)
        return
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise PipelineIdentityError(f"{where}: keys must be strings, got {key!r}")
            _check_option(item, f"{where}.{key}", depth + 1)
        return
    raise PipelineIdentityError(f"{where}: {kind.__name__} is not a plain JSON value")


# ---------------------------------------------------------------- paths

def _redirected(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _real_path(path, label: str, directory: bool) -> Path:
    """Absolute path whose components (and ancestors) are real entries; must exist with the right type."""
    if not isinstance(path, (str, os.PathLike)):
        raise PipelineIdentityError(f"{label}: path must be a string or path")
    absolute = Path(os.path.abspath(path))
    for entry in (absolute, *absolute.parents):
        try:
            info = os.lstat(entry)
        except FileNotFoundError:
            raise PipelineIdentityError(f"{label}: {entry} does not exist") from None
        if _redirected(info):
            raise PipelineIdentityError(f"{label}: {entry} is a symlink or reparse point")
    info = os.lstat(absolute)
    if directory and not stat.S_ISDIR(info.st_mode):
        raise PipelineIdentityError(f"{label}: {absolute} is not a directory")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise PipelineIdentityError(f"{label}: {absolute} is not a regular file")
    return absolute


def _walk(root: Path, label: str) -> list:
    """[(name, path)] of every regular file below root, sorted by POSIX name."""
    found, stack = [], [(root, f"{label}/")]
    while stack:
        directory, prefix = stack.pop()
        # Checked again right before listing: a directory swapped for a link since it was found is refused.
        info = os.lstat(directory)
        if _redirected(info) or not stat.S_ISDIR(info.st_mode):
            raise PipelineIdentityError(f"{prefix.rstrip('/')} is no longer a real directory")
        with os.scandir(directory) as entries:
            for entry in entries:
                info = os.lstat(entry.path)
                name = prefix + entry.name
                if "\\" in entry.name or "\0" in entry.name:
                    # Such names are not portable POSIX paths; validate_pipeline_identity refuses them too.
                    raise PipelineIdentityError(f"{name!r} contains a backslash or NUL")
                if _redirected(info):
                    raise PipelineIdentityError(f"{name} is a symlink or reparse point")
                if stat.S_ISDIR(info.st_mode):
                    stack.append((Path(entry.path), name + "/"))
                elif stat.S_ISREG(info.st_mode):
                    found.append((name, Path(entry.path)))
                else:
                    raise PipelineIdentityError(f"{name} is not a regular file or directory")
    return sorted(found)


def _same_file(a, b) -> bool:
    """Same size and mtime; on POSIX also the same device/inode (Windows stat/fstat device ids can differ)."""
    same = (a.st_size, a.st_mtime_ns) == (b.st_size, b.st_mtime_ns)
    return same and (os.name != "posix" or (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino))


def _hash(path: Path, name: str, remaining: int) -> tuple[int, str]:
    """Size and SHA-256 in blocks of at most 1 MiB, never reading more than remaining + 1 bytes. The file
    must not change while it is read, and the path must still name the same file afterwards."""
    before = os.lstat(path)
    if _redirected(before) or not stat.S_ISREG(before.st_mode):
        raise PipelineIdentityError(f"{name} is no longer a regular file")
    if before.st_size > remaining:
        raise PipelineIdentityError(f"{name} exceeds the remaining max_total_bytes budget")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (
                os.name == "posix" and (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise PipelineIdentityError(f"{name} was replaced while it was opened")
        digest, size = hashlib.sha256(), 0
        while block := stream.read(min(BLOCK, remaining + 1 - size)):
            size += len(block)
            if size > remaining:   # grew past the budget: stop at remaining + 1 bytes
                raise PipelineIdentityError(f"{name} grew beyond the max_total_bytes budget while it was read")
            digest.update(block)
        after = os.fstat(stream.fileno())
    named = os.lstat(path)   # the path must still name the file that was read
    if size != opened.st_size or not _same_file(after, opened) or _redirected(named) \
            or not stat.S_ISREG(named.st_mode) or not _same_file(named, opened):
        raise PipelineIdentityError(f"{name} changed or was replaced while it was read")
    return size, digest.hexdigest()


# ---------------------------------------------------------------- identity

def _scan(declared_roots: dict, declared_files: dict, max_files: int, max_total_bytes: int) -> list:
    # Every pass validates the declared paths and their ancestors again (a root swapped for a link is refused).
    roots = {label: _real_path(path, f"root {label}", True) for label, path in declared_roots.items()}
    files = {label: _real_path(path, f"file {label}", False) for label, path in declared_files.items()}
    planned = [(label, path) for label, path in files.items()]
    for label, root in roots.items():
        planned += _walk(root, label)
    if len(planned) > max_files:
        raise PipelineIdentityError(f"{len(planned)} files exceed max_files {max_files}")
    total = sum(os.lstat(path).st_size for _, path in planned)
    if total > max_total_bytes:
        raise PipelineIdentityError(f"{total} bytes exceed max_total_bytes {max_total_bytes}")
    _after_preflight()
    records, read = [], 0
    for name, path in sorted(planned):
        size, digest = _hash(path, name, max_total_bytes - read)   # enforced before and while reading
        read += size
        records.append({"name": name, "bytes": size, "sha256": digest})
    return records


HEX64 = re.compile(r"[0-9a-f]{64}")


def _check_name(name: str, roots: set, where: str) -> None:
    """A file name is '<root label>/<relative POSIX path>' or a standalone file label (not a root label)."""
    if type(name) is not str or not name or "\\" in name or "\0" in name:
        raise PipelineIdentityError(f"{where}: name must be a non-empty string without backslash or NUL")
    if "/" not in name:
        if not LABEL.fullmatch(name):
            raise PipelineIdentityError(f"{where}: standalone file label {name!r} is invalid")
        if name in roots:
            raise PipelineIdentityError(f"{where}: {name!r} is both a root label and a file label")
        return
    root, _, relative = name.partition("/")
    if root not in roots:
        raise PipelineIdentityError(f"{where}: {name!r} is not below a declared root")
    if any(part in ("", ".", "..") for part in relative.split("/")):
        raise PipelineIdentityError(f"{where}: {name!r} is not a normalized relative POSIX path")


def validate_pipeline_identity(record) -> dict:
    """Strictly validate a recorded UIE_PIPELINE_IDENTITY1 and return an independent normalized copy.

    Pure: no filesystem access. It proves only that the record is well formed and that pipelineKey is the
    canonical hash of its identity; it does not rescan the inputs or show that all dependencies were declared.
    """
    if type(record) is not dict or set(record) != {"format", "pipelineKey", "identity"}:
        raise PipelineIdentityError("record must be exactly {format, pipelineKey, identity}")
    if record["format"] != FORMAT:
        raise PipelineIdentityError(f"record format must be {FORMAT}")
    key = record["pipelineKey"]
    if type(key) is not str or not HEX64.fullmatch(key):
        raise PipelineIdentityError("pipelineKey must be 64 lowercase hex")
    identity = record["identity"]
    if type(identity) is not dict or set(identity) != {"format", "roots", "files", "options"}:
        raise PipelineIdentityError("identity must be exactly {format, roots, files, options}")
    if identity["format"] != INPUTS_FORMAT:
        raise PipelineIdentityError(f"identity format must be {INPUTS_FORMAT}")
    roots = identity["roots"]
    if type(roots) is not list or not all(type(r) is str and LABEL.fullmatch(r) for r in roots) \
            or roots != sorted(set(roots)):
        raise PipelineIdentityError("roots must be a sorted list of unique valid labels")
    files = identity["files"]
    if type(files) is not list or (not roots and not files):
        raise PipelineIdentityError("files must be a list, and roots or files must not both be empty")
    names, total = [], 0
    for i, entry in enumerate(files):
        where = f"files[{i}]"
        if type(entry) is not dict or set(entry) != {"name", "bytes", "sha256"}:
            raise PipelineIdentityError(f"{where} must be exactly {{name, bytes, sha256}}")
        _check_name(entry["name"], set(roots), where)
        if type(entry["bytes"]) is not int or not 0 <= entry["bytes"] <= MAX_SAFE:
            raise PipelineIdentityError(f"{where}.bytes must be a safe integer >= 0")
        if type(entry["sha256"]) is not str or not HEX64.fullmatch(entry["sha256"]):
            raise PipelineIdentityError(f"{where}.sha256 must be 64 lowercase hex")
        total += entry["bytes"]
        names.append(entry["name"])
    if names != sorted(set(names)):
        raise PipelineIdentityError("file names must be sorted and unique")
    name_set = set(names)
    for name in names:
        parts = name.split("/")
        if any("/".join(parts[:i]) in name_set for i in range(1, len(parts))):
            raise PipelineIdentityError(f"file {name!r} has a file as an ancestor")
    if total > MAX_SAFE:
        raise PipelineIdentityError("total bytes exceed the safe integer range")
    if type(identity["options"]) is not dict:
        raise PipelineIdentityError("options must be a plain JSON object")
    _check_option(identity["options"], "options")
    if hashlib.sha256(canonical_json(identity)).hexdigest() != key:
        raise PipelineIdentityError("pipelineKey is not the canonical hash of the identity")
    return json.loads(json.dumps(record, allow_nan=False))   # independent copy


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def create_pipeline_identity(roots, files, options, *, max_files=20000, max_total_bytes=8 * 1024 ** 3) -> dict:
    for name, value in (("max_files", max_files), ("max_total_bytes", max_total_bytes)):
        if type(value) is not int or not 0 <= value <= MAX_SAFE:
            raise PipelineIdentityError(f"{name} must be a safe integer >= 0")
    if type(roots) is not dict or type(files) is not dict:
        raise PipelineIdentityError("roots and files must be dicts of label -> path")
    if not roots and not files:
        raise PipelineIdentityError("declare at least one root or file")
    for label in [*roots, *files]:
        if type(label) is not str or not LABEL.fullmatch(label):
            raise PipelineIdentityError(f"label {label!r} must match [a-z][a-z0-9_-]{{0,63}}")
    if set(roots) & set(files):
        raise PipelineIdentityError(f"labels used for both a root and a file: {sorted(set(roots) & set(files))}")
    if type(options) is not dict:
        raise PipelineIdentityError("options must be a plain JSON object")
    _check_option(options, "options")
    options = copy.deepcopy(options)   # later caller mutation cannot change the result

    root_paths = {label: _real_path(path, f"root {label}", True) for label, path in roots.items()}
    file_paths = {label: _real_path(path, f"file {label}", False) for label, path in files.items()}
    real_roots = {label: path.resolve() for label, path in root_paths.items()}
    for a, pa in real_roots.items():
        for b, pb in real_roots.items():
            if a < b and (pa == pb or pa.is_relative_to(pb) or pb.is_relative_to(pa)):
                raise PipelineIdentityError(f"roots {a} and {b} overlap")
    seen = {}
    for label, path in file_paths.items():
        real = path.resolve()
        if real in seen:
            raise PipelineIdentityError(f"files {seen[real]} and {label} are the same file")
        seen[real] = label
        inside = [r for r, root in real_roots.items() if real.is_relative_to(root)]
        if inside:
            raise PipelineIdentityError(f"file {label} lies inside root {inside[0]} and would be counted twice")

    try:
        first = _scan(roots, files, max_files, max_total_bytes)
        _after_first_pass()
        # Second full pass: same paths, sizes and contents, or the inputs were not frozen.
        second = _scan(roots, files, max_files, max_total_bytes)
    except OSError as exc:   # e.g. a file removed between listing and reading
        raise PipelineIdentityError(f"inputs could not be read consistently: {exc}") from None
    if second != first:
        raise PipelineIdentityError("inputs changed between the two passes; freeze them and try again")
    identity = {"format": INPUTS_FORMAT, "roots": sorted(roots), "files": first, "options": options}
    return {"format": FORMAT, "pipelineKey": hashlib.sha256(canonical_json(identity)).hexdigest(), "identity": identity}

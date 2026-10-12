"""Production staging adapter for warm_worker: copies one immutable prepared plan and one frozen runtime profile
into a fresh attempt and proves, by scanning the copy, that it is exactly the declared identity.

    prepare = make_prepare(plan_dir, profile_dir, profile, max_copy_bytes=3 GiB, min_free_bytes=1 GiB,
                           timeout=180)
    warm_worker.run_one(queue, attempts_dir, prepare=prepare)

profile = {"identity": UIE_PIPELINE_IDENTITY1, "runtime": {roots, files, links, command, env, cwd, executables}}
and identity.identity.options.runtime must equal runtime exactly, so the path mapping, aliases, executable
modes, argv and env overrides are all part of the pipeline key. Sources are only read. No process is
started, nothing is queued or published. See RUNTIME-ADAPTER.md.
"""
from __future__ import annotations

import copy
import hashlib
import math
import os
import re
import shutil
import stat
import time
from pathlib import Path

import verify_rendered_plan as vrp
from owned_process import NONCE_ENV
from pipeline_identity import LABEL, PipelineIdentityError, create_pipeline_identity, validate_pipeline_identity
from warm_worker import PermanentPrepareError

GIB = 1024 ** 3
BLOCK = 1 << 20
HEX64 = re.compile(r"[0-9a-f]{64}")
RUNTIME_KEYS = {"roots", "files", "links", "command", "env", "cwd", "executables"}
MARKERS = {"RUNTIME", "PLAN", "OUTPUT"}
PLACEHOLDER = re.compile(r"@([A-Za-z0-9_]*)@")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SUBDIRS = ("plan", "runtime", "output")


class AdapterError(Exception):
    """Transient staging failure (deadline, disk reserve, I/O); warm_worker retries in a fresh attempt."""


def _permanent(message: str) -> PermanentPrepareError:
    return PermanentPrepareError(message)


# ------------------------------------------------------------------ static validation

def _rel(value, where: str) -> str:
    """A portable normalized relative POSIX path. ':' is refused everywhere: 'C:/x' and 'C:x' would escape
    the base when joined on Windows. POSIX allows ':' in names; this contract is intentionally stricter."""
    if type(value) is not str or not value or "\\" in value or "\0" in value or ":" in value \
            or value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/")):
        raise _permanent(f"{where}: {value!r} is not a normalized relative POSIX path (no ':', '\\', '..')")
    return value


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base + "/")


def _related(a: str, b: str) -> bool:
    return _under(a, b) or _under(b, a)


def _ancestors(path: str) -> list:
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _resolve(path: str, links: dict) -> str:
    """One alias hop (chains are refused statically)."""
    for link, target in links.items():
        if _under(path, link):
            return target + path[len(link):]
    return path


def _placeholders(text: str, where: str) -> None:
    for match in PLACEHOLDER.finditer(text):
        if match.group(1) not in MARKERS:
            raise _permanent(f"{where}: unknown placeholder {match.group(0)!r}")


def _check_profile(profile) -> tuple:
    """(identity, runtime, layout) or PermanentPrepareError. Pure: no filesystem access."""
    if type(profile) is not dict or set(profile) != {"identity", "runtime"}:
        raise _permanent("profile must be exactly {identity, runtime}")
    try:
        identity = validate_pipeline_identity(profile["identity"])
    except PipelineIdentityError as exc:
        raise _permanent(f"profile identity is invalid: {exc}") from None
    runtime = profile["runtime"]
    if type(runtime) is not dict or set(runtime) != RUNTIME_KEYS:
        raise _permanent(f"runtime must be exactly {sorted(RUNTIME_KEYS)}")
    roots, files, links = runtime["roots"], runtime["files"], runtime["links"]
    if not all(type(table) is dict for table in (roots, files, links)):
        raise _permanent("runtime roots, files and links must be objects")
    declared = []
    for kind, table in (("root", roots), ("file", files)):
        for label, path in table.items():
            if type(label) is not str or not LABEL.fullmatch(label):
                raise _permanent(f"runtime {kind} label {label!r} is invalid")
            declared.append((f"{kind} {label}", _rel(path, f"runtime {kind} {label}")))
    for i, (a, pa) in enumerate(declared):
        for b, pb in declared[i + 1:]:
            if _related(pa, pb):
                raise _permanent(f"{a} and {b} overlap ({pa!r}, {pb!r})")
    if set(roots) != set(identity["identity"]["roots"]):
        raise _permanent("runtime roots differ from the identity's roots")
    if set(files) != {f["name"] for f in identity["identity"]["files"] if "/" not in f["name"]}:
        raise _permanent("runtime files differ from the identity's standalone files")

    copies = []   # (identity name, runtime-relative path, bytes, sha256)
    for entry in identity["identity"]["files"]:
        name = entry["name"]
        if "/" in name:
            label, rest = name.split("/", 1)
            path = f"{roots[label]}/{rest}"
        else:
            path = files[name]
        # The constructed copy path is checked too: an identity name below a root may hold e.g. ':'.
        copies.append((name, _rel(path, f"identity file {name!r}"), entry["bytes"], entry["sha256"]))
    copied_files = {path for _, path, _, _ in copies}
    # Directories the copy actually creates: every root and every ancestor of a root or a copied file
    # (e.g. 'client' above 'client/mods'). Nothing else, so an uncopied path like 'client/saves' stays refused.
    created_dirs = set(roots.values())
    for path in [*roots.values(), *copied_files]:
        created_dirs.update(_ancestors(path))

    for link, target in links.items():
        _rel(link, "runtime link")
        _rel(target, f"runtime link {link!r} target")
        for _, path in declared:
            if _related(link, path):
                raise _permanent(f"alias {link!r} overlaps the declared path {path!r}")
        for other in links:
            if other != link and _related(link, other):
                raise _permanent(f"aliases {link!r} and {other!r} overlap")
            if _related(target, other):
                raise _permanent(f"alias {link!r} targets another alias (chains and cycles are refused)")
        if target not in copied_files and not any(_under(path, target) for path in copied_files):
            raise _permanent(f"alias {link!r} target {target!r} is not a declared copied file or directory")
    structure = set(created_dirs)   # plus the parents created for aliases (possible cwd values)
    for link in links:
        structure.update(_ancestors(link))

    command, env = runtime["command"], runtime["env"]
    if type(command) is not list or not command or not all(type(a) is str and "\0" not in a for a in command) \
            or not command[0]:
        raise _permanent("runtime command must be a non-empty list of strings without NUL (no shell)")
    for i, arg in enumerate(command):
        _placeholders(arg, f"command[{i}]")
        if arg.startswith("@RUNTIME@/") and "@" not in arg[len("@RUNTIME@/"):]:
            path = _rel(arg[len("@RUNTIME@/"):], f"command[{i}]")
            if _resolve(path, links) not in copied_files | created_dirs:
                raise _permanent(f"command[{i}] {arg!r} is not a copied file or a directory the copy creates")
    if type(env) is not dict:
        raise _permanent("runtime env must be an object")
    for key, value in env.items():
        if type(key) is not str or not ENV_NAME.fullmatch(key) or key == NONCE_ENV:
            raise _permanent(f"runtime env name {key!r} is invalid or reserved")
        if type(value) is not str or "\0" in value:
            raise _permanent(f"runtime env {key} must be a string without NUL")
        _placeholders(value, f"env {key}")
    cwd = _rel(runtime["cwd"], "runtime cwd")
    if cwd not in structure or any(_under(cwd, link) for link in links):   # not at or through an alias
        raise _permanent(f"runtime cwd {cwd!r} is not a real directory of the copied runtime")
    executables = runtime["executables"]
    if type(executables) is not list or len(set(map(str, executables))) != len(executables):
        raise _permanent("runtime executables must be a list of unique paths")
    for path in executables:
        if _rel(path, "runtime executable") not in copied_files:
            raise _permanent(f"executable {path!r} is not a declared copied file")

    if identity["identity"]["options"].get("runtime") != runtime:
        raise _permanent("identity.options.runtime must equal the runtime record exactly")
    return identity, runtime, {"copies": copies, "executables": set(executables)}


def _canonical_dir(value, label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not os.path.isabs(value):
        raise _permanent(f"{label} must be an absolute path")
    path = Path(value)
    if os.path.realpath(path) != str(path) or not os.path.isdir(path):
        raise _permanent(f"{label} must be a canonical existing directory without links")
    return path


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


# ------------------------------------------------------------------ copying

class _Budget:
    def __init__(self, limit: int, deadline: float, min_free: int):
        self.left, self.deadline, self.min_free = limit, deadline, min_free

    def tick(self, where: str) -> None:
        if time.monotonic() > self.deadline:
            raise AdapterError(f"staging deadline exceeded ({where})")

    def reserve(self, size: int, directory: Path) -> None:
        if size > self.left:
            raise _permanent(f"max_copy_bytes budget exceeded by {size - self.left} bytes")
        if shutil.disk_usage(directory).free < size + self.min_free:
            raise AdapterError("free disk reserve would be exhausted")
        self.left -= size


def _mkdirs(base: Path, relative_dir: str) -> None:
    current = base
    for part in relative_dir.split("/") if relative_dir else ():
        current = current / part
        try:
            os.mkdir(current, 0o700)
            os.chmod(current, 0o700)
        except FileExistsError:
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _permanent(f"{current} is not a real directory") from None


def _copy(source: Path, target: Path, size, digest: str, mode: int, budget: _Budget) -> None:
    """Copy a regular file in 1 MiB blocks, hashing while copying; never more than size (+1 to detect growth)."""
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _permanent(f"{source} is not a regular file")
        if size is not None and info.st_size != size:
            raise _permanent(f"{source} has {info.st_size} bytes, the identity says {size}")
        size = info.st_size
        budget.reserve(size, target.parent)
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                      | getattr(os, "O_BINARY", 0), 0o600)
        try:
            sha, copied = hashlib.sha256(), 0
            while True:
                budget.tick(f"copying {source.name}")
                block = os.read(fd, min(BLOCK, size + 1 - copied))
                if not block:
                    break
                copied += len(block)
                if copied > size:
                    raise _permanent(f"{source} grew while it was copied")
                sha.update(block)
                view = memoryview(block)
                while view:
                    view = view[os.write(out, view):]
            if hasattr(os, "fchmod"):
                os.fchmod(out, mode)
        finally:
            os.close(out)
    finally:
        os.close(fd)
    if not hasattr(os, "fchmod"):
        os.chmod(target, mode)
    if copied != size or sha.hexdigest() != digest:
        raise _permanent(f"{source} does not match its recorded SHA-256")


def _scan(base: Path, runtime: dict, identity: dict) -> dict:
    files = identity["identity"]["files"]
    try:
        return create_pipeline_identity({label: base / path for label, path in runtime["roots"].items()},
                                        {label: base / path for label, path in runtime["files"].items()},
                                        identity["identity"]["options"], max_files=len(files),
                                        max_total_bytes=sum(f["bytes"] for f in files))
    except PipelineIdentityError as exc:
        raise _permanent(f"identity scan of {base} failed: {exc}") from None


# ------------------------------------------------------------------ API

def make_prepare(plan_dir, profile_dir, profile, *, max_copy_bytes=3 * GIB, min_free_bytes=GIB, timeout=180.0):
    """A warm_worker prepare callback for one immutable prepared plan and one frozen runtime profile.

    Here only directory metadata is read (plan_dir and profile_dir must be canonical existing directories,
    via realpath/isdir) and the profile record is validated without reading any file contents. Every
    content check (plan hashes, identity scans, aliases) runs again in each callback."""
    for name, value in (("max_copy_bytes", max_copy_bytes), ("min_free_bytes", min_free_bytes)):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) \
            or not 0 < timeout <= 86400:
        raise ValueError("timeout must be a number in (0, 86400] seconds")
    plan_dir = _canonical_dir(plan_dir, "plan_dir")
    profile_dir = _canonical_dir(profile_dir, "profile_dir")
    if _overlap(plan_dir, profile_dir):
        raise _permanent("plan_dir and profile_dir overlap")
    identity, runtime, layout = _check_profile(copy.deepcopy(profile))
    settings = {"plan": plan_dir, "profile": profile_dir, "identity": identity, "runtime": runtime,
                "layout": layout, "max_copy_bytes": max_copy_bytes, "min_free_bytes": min_free_bytes,
                "timeout": float(timeout)}

    def prepare(claim, attempt_dir):
        return _prepare(settings, claim, attempt_dir)
    return prepare


def _prepare(s: dict, claim, attempt_dir) -> dict:
    budget = _Budget(s["max_copy_bytes"], time.monotonic() + s["timeout"], s["min_free_bytes"])
    plan_dir, profile_dir, identity, runtime = s["plan"], s["profile"], s["identity"], s["runtime"]
    attempt = _canonical_dir(attempt_dir, "attempt_dir")
    if _overlap(attempt, plan_dir) or _overlap(attempt, profile_dir):
        raise _permanent("the attempt directory overlaps the plan or the profile")
    payload = claim.get("payload") if type(claim) is dict else None
    expected = payload.get("planSha256") if type(payload) is dict else None
    if type(expected) is not str or not HEX64.fullmatch(expected):
        raise _permanent("the claim payload needs planSha256 (64 lowercase hex)")

    # 1. The source plan: exact inventory against COMPLETE, the expected plan, the profile's identity.
    try:
        _, _, binding = vrp._verify_plan(plan_dir, expected, "COMPLETE")
        complete_raw = vrp._read(plan_dir, "COMPLETE", vrp.LIMIT_JSON)
        tracked = vrp._json(complete_raw, "COMPLETE")["files"]
        staged = validate_pipeline_identity(vrp._json(vrp._read(plan_dir, vrp.IDENTITY_FILE, vrp.LIMIT_JSON),
                                                      vrp.IDENTITY_FILE))
        plan_bytes = sum(os.lstat(vrp._path(plan_dir, relative)).st_size for relative in tracked)
    except (vrp.RenderVerificationError, PipelineIdentityError) as exc:
        raise _permanent(f"source plan is invalid: {exc}") from None
    if binding["pipelineKeySource"] != "declared-inputs-sha256" or binding["pipelineKey"] != identity["pipelineKey"] \
            or staged != identity:
        raise _permanent("the plan is not bound to this profile's declared identity")
    runtime_bytes = sum(f["bytes"] for f in identity["identity"]["files"])
    if plan_bytes + 2 * len(complete_raw) + runtime_bytes > s["max_copy_bytes"]:
        raise _permanent("plan and runtime exceed max_copy_bytes")
    budget.tick("source plan checked")

    # 2. The source runtime: a fresh scan must reproduce the recorded identity; aliases must point exactly.
    if _scan(profile_dir, runtime, identity) != identity:
        raise _permanent("the source profile does not match its recorded identity")
    budget.tick("source profile scanned")
    for link, target in runtime["links"].items():
        source = profile_dir / link
        for ancestor in _ancestors(link):
            info = os.lstat(profile_dir / ancestor)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _permanent(f"alias {link!r} lies below a link or non-directory")
        if not stat.S_ISLNK(os.lstat(source).st_mode) \
                or os.path.normpath(os.path.join(os.path.dirname(source), os.readlink(source))) \
                != str(profile_dir / target):
            raise _permanent(f"source alias {link!r} does not point exactly to {target!r}")

    # 3. Fresh, exclusive attempt subdirectories (an existing one is refused; partial attempts are evidence).
    dirs = {}
    for name in SUBDIRS:
        try:
            os.mkdir(attempt / name, 0o700)
        except FileExistsError:
            raise _permanent(f"attempt subdirectory {name}/ already exists; attempts are never reused") from None
        os.chmod(attempt / name, 0o700)
        dirs[name] = attempt / name
    plan, runtime_dir = dirs["plan"], dirs["runtime"]

    # 4. Plan: tracked files, COMPLETE, then PLAN-COMPLETE.json byte-identical to COMPLETE.
    complete_sha = hashlib.sha256(complete_raw).hexdigest()
    for relative, digest in sorted(tracked.items()):
        _mkdirs(plan, os.path.dirname(relative))
        _copy(vrp._path(plan_dir, relative), plan / relative, None, digest, 0o644, budget)
    for name in ("COMPLETE", "PLAN-COMPLETE.json"):
        _copy(plan_dir / "COMPLETE", plan / name, len(complete_raw), complete_sha, 0o644, budget)

    # 5. Runtime: declared files only, hashed while copied; empty roots; aliases as relative links.
    for name, path, size, digest in s["layout"]["copies"]:
        if "/" in name:
            label, rest = name.split("/", 1)
            source = profile_dir / runtime["roots"][label] / rest
        else:
            source = profile_dir / runtime["files"][name]
        _mkdirs(runtime_dir, os.path.dirname(path))
        mode = 0o755 if path in s["layout"]["executables"] else 0o644
        _copy(source, runtime_dir / path, size, digest, mode, budget)
    for path in runtime["roots"].values():
        _mkdirs(runtime_dir, path)
    for link, target in runtime["links"].items():
        _mkdirs(runtime_dir, os.path.dirname(link))
        os.symlink(os.path.relpath(target, os.path.dirname(link) or "."), runtime_dir / link)

    # 6. The copy is checked on its own: identity scan, aliases, modes, cwd, the staged plan.
    budget.tick("runtime copied")
    current = _scan(runtime_dir, runtime, identity)
    if current != identity:
        raise _permanent("the copied runtime does not match the declared identity")
    budget.tick("copied runtime scanned")
    for link, target in runtime["links"].items():
        if os.path.realpath(runtime_dir / link) != str(runtime_dir / target):
            raise _permanent(f"copied alias {link!r} does not resolve to {target!r}")
    if os.name == "posix":
        for _, path, _, _ in s["layout"]["copies"]:
            expected_mode = 0o755 if path in s["layout"]["executables"] else 0o644
            if stat.S_IMODE(os.lstat(runtime_dir / path).st_mode) != expected_mode:
                raise _permanent(f"copied {path!r} has the wrong mode")
    cwd = runtime_dir / runtime["cwd"]
    for part in [runtime["cwd"], *_ancestors(runtime["cwd"])]:
        info = os.lstat(runtime_dir / part)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _permanent("runtime cwd is not a real directory in the copy")
    try:
        vrp._verify_plan(plan, expected, "PLAN-COMPLETE.json")
    except vrp.RenderVerificationError as exc:
        raise _permanent(f"staged plan is invalid: {exc}") from None
    if os.path.lexists(plan / "results") or (plan / "COMPLETE").read_bytes() != (plan / "PLAN-COMPLETE.json").read_bytes():
        raise _permanent("staged plan is not fresh or its markers differ")

    # 7. Literal markers expanded with this attempt's directories (single pass, no shell).
    values = {"RUNTIME": str(runtime_dir), "PLAN": str(plan), "OUTPUT": str(dirs["output"])}
    expand = lambda text: PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
    env = {key: value for key, value in os.environ.items() if key != NONCE_ENV}
    env.update({key: expand(value) for key, value in runtime["env"].items()})
    return {"planDir": str(plan), "cwd": str(cwd), "command": [expand(arg) for arg in runtime["command"]],
            "env": env, "currentIdentity": current, "expectedPlanSha256": expected}

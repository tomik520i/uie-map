"""Linux-only ownership of an offline worker's process tree through a durable attempt scope (stdlib only).

    scope = create_scope()        # {"nonce", "bootId", "notBeforeTicks"}; persist it durably BEFORE launch
                                  # isolate_cgroup=True adds "cgroup": only processes inside it are considered
    process = launch_owned(command, cwd=..., log=open(path, "wb"), scope=scope)
    inspect_owned(scope)          # [{"pid": ...}] live processes of this UID carrying the exact nonce entry
    stop_owned(scope, term_timeout=10, kill_timeout=10, process=process)    # or without process (recovery)

Ownership is the exact environment entry UIE_ATTEMPT_NONCE=<nonce>, which every descendant inherits unless it
replaces its environment. Each candidate is opened as a pidfd BEFORE its /proc data is read, and is only
signalled through that pidfd, so a stale or reused PID can never hit a replacement process. No process-group
number is ever signalled. A process that started before the scope's notBeforeTicks cannot have inherited the
nonce (it was created afterwards) and is skipped without reading its environment; any newer same-UID process
whose environment is unreadable makes the scan fail. Environments are only searched for the exact entry and
never returned, logged or stored. See OWNED-PROCESS.md for the guarantees and the limits.
"""
from __future__ import annotations

import math
import os
import re
import secrets
import select
import signal
import stat
import subprocess
import sys
import time
import weakref

NONCE_ENV = "UIE_ATTEMPT_NONCE"
NONCE = re.compile(r"[0-9a-f]{32}")
BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SCOPE_KEYS = {"nonce", "bootId", "notBeforeTicks"}
MAX_TICKS = 2 ** 53 - 1
ROUNDING_TICKS = 2          # margin for /proc/uptime's two-decimal truncation
MAX_ENVIRON_BYTES = 16 * 1024 * 1024
MAX_TIMEOUT = 600.0
POLL_INTERVAL = 0.05

_POPEN = subprocess.Popen   # the real class, kept even if a test replaces subprocess.Popen
_LAUNCHED = weakref.WeakKeyDictionary()   # Popen handle -> immutable copy of its launch scope


class OwnedProcessError(Exception):
    """Invalid arguments, or the owned processes cannot be handled safely."""


class UnsupportedPlatformError(OwnedProcessError):
    """Not Linux, or pidfd_open / pidfd_send_signal / poll are unavailable. Nothing was launched."""


class StaleScopeError(OwnedProcessError):
    """The scope belongs to another boot. Its processes cannot exist now; the caller must handle that."""


class ScanError(OwnedProcessError):
    """/proc could not be scanned completely; ownership is unknown, so nothing is reported as stopped."""


class CleanupError(OwnedProcessError):
    """Owned processes are still alive after the bounded TERM/KILL cleanup."""

    def __init__(self, message: str, remaining: list):
        super().__init__(message)
        self.remaining = remaining


# ------------------------------------------------------------------ validation

def _check_scope_schema(scope) -> None:
    if type(scope) is not dict or set(scope) not in (SCOPE_KEYS, SCOPE_KEYS | {"cgroup"}):
        raise OwnedProcessError("scope must be exactly {nonce, bootId, notBeforeTicks} (+ optional cgroup) "
                                "from create_scope()")
    if type(scope["nonce"]) is not str or not NONCE.fullmatch(scope["nonce"]):
        raise OwnedProcessError("scope nonce must be 32 lowercase hex characters")
    if type(scope["bootId"]) is not str or not BOOT_ID.fullmatch(scope["bootId"]):
        raise OwnedProcessError("scope bootId must be a lowercase Linux boot id")
    ticks = scope["notBeforeTicks"]
    if type(ticks) is not int or not 0 <= ticks <= MAX_TICKS:
        raise OwnedProcessError("scope notBeforeTicks must be an integer between 0 and 2^53-1")
    if "cgroup" in scope and not _valid_cgroup(scope["cgroup"]):
        raise OwnedProcessError("scope cgroup must be a non-root normalized absolute UTF-8 cgroup v2 path")


def _check_scope_current(scope) -> None:
    """After _require_platform: the scope must belong to this boot and its cutoff must not lie in the future."""
    if scope["bootId"] != _boot_id():
        raise StaleScopeError("scope belongs to another boot; none of its processes can still exist")
    if scope["notBeforeTicks"] > _uptime_ticks():
        raise OwnedProcessError("scope notBeforeTicks lies in the future of this boot")


def _check_process(process, scope) -> None:
    if process is None:
        return
    if not isinstance(process, _POPEN) or _LAUNCHED.get(process) != scope:
        raise OwnedProcessError("process must be the Popen that launch_owned returned for this scope")


def _timeout(value, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) \
            or not 0 <= value <= MAX_TIMEOUT:
        raise OwnedProcessError(f"{name} must be a number between 0 and {MAX_TIMEOUT:g} seconds")
    return float(value)


def _require_platform() -> None:
    if not sys.platform.startswith("linux"):
        raise UnsupportedPlatformError("owned processes need Linux (/proc and pidfd)")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal") or not hasattr(select, "poll"):
        raise UnsupportedPlatformError("os.pidfd_open, signal.pidfd_send_signal and select.poll are required")
    if not os.path.isdir("/proc/self"):
        raise UnsupportedPlatformError("/proc is not mounted")
    try:
        pidfd = os.pidfd_open(os.getpid())
    except OSError as exc:
        raise UnsupportedPlatformError(f"pidfd_open is not supported by this kernel ({exc.strerror})") from None
    try:
        signal.pidfd_send_signal(pidfd, 0)
    except OSError as exc:
        raise UnsupportedPlatformError(f"pidfd_send_signal is not supported by this kernel ({exc.strerror})") from None
    finally:
        os.close(pidfd)


# ------------------------------------------------------------------ boot clock

def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", "rb") as handle:
            value = handle.read(128).decode("ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise UnsupportedPlatformError(f"cannot read the boot id ({exc})") from None
    if not BOOT_ID.fullmatch(value):
        raise UnsupportedPlatformError("unexpected boot id format")
    return value


def _clock_ticks() -> int:
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError) as exc:
        raise UnsupportedPlatformError(f"SC_CLK_TCK is unavailable ({exc})") from None
    if type(hz) is not int or hz <= 0:
        raise UnsupportedPlatformError("SC_CLK_TCK is not a positive integer")
    return hz


def _uptime_ticks() -> int:
    """Time since boot in clock ticks, rounded down (never later than the true time)."""
    try:
        with open("/proc/uptime", "rb") as handle:
            first = handle.read(256).split()[0].decode("ascii")
    except (OSError, IndexError, UnicodeDecodeError) as exc:
        raise UnsupportedPlatformError(f"cannot read /proc/uptime ({exc})") from None
    whole, _, fraction = first.partition(".")
    if not whole.isdigit() or (fraction and not fraction.isdigit()):
        raise UnsupportedPlatformError("unexpected /proc/uptime format")
    centiseconds = int(whole) * 100 + int((fraction + "00")[:2])   # truncates extra digits: a lower bound
    return centiseconds * _clock_ticks() // 100


def _cgroup_bytes(pid: int) -> bytes:
    with open(f"/proc/{pid}/cgroup", "rb") as stream:
        return stream.read(65536)


def _cgroup_path(pid: int) -> str:
    """The unified (v2) cgroup path of pid, decoded losslessly.

    The kernel prints the path raw (any bytes but NUL and '/'-separated names). It is decoded as UTF-8 with
    surrogateescape, which is injective, so string comparisons are exact byte comparisons: a non-UTF-8 path
    never matches a recorded (valid UTF-8) scope path and never raises a decoding error. Anything but exactly
    one '0::/...' row (cgroup v1 only, or a newline smuggled into a cgroup name) raises ScanError."""
    unified = [row[3:] for row in _cgroup_bytes(pid).split(b"\n") if row.startswith(b"0::")]
    if len(unified) != 1 or not unified[0].startswith(b"/"):
        raise ScanError(f"process {pid}: a single unified cgroup v2 membership is required")
    return unified[0].decode("utf-8", "surrogateescape")


def _in_cgroup(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _valid_cgroup(path) -> bool:
    """A recordable cgroup: absolute, normalized, not the root, valid UTF-8, no NUL, backslash or newline."""
    if type(path) is not str or not path.startswith("/") or any(c in path for c in "\0\\\n") \
            or any(p in ("", ".", "..") for p in path[1:].split("/")):
        return False
    try:
        path.encode("utf-8")   # refuses surrogate-escaped (non-UTF-8) paths
    except UnicodeEncodeError:
        return False
    return True


def check_cgroup_isolation() -> str:
    """The calling process's cgroup v2 path if isolate_cgroup can be used here; else UnsupportedPlatformError.

    Needs Linux with pidfd, a single unified cgroup v2 membership, and a recordable non-root path (e.g. the
    worker's own systemd service). Nothing is started or changed."""
    _require_platform()
    try:
        path = _cgroup_path(os.getpid())
    except (OSError, ScanError) as exc:
        raise UnsupportedPlatformError(f"cgroup v2 isolation is unavailable: {exc}") from None
    if not _valid_cgroup(path):
        raise UnsupportedPlatformError("cgroup v2 isolation needs a non-root cgroup with a normalized UTF-8 path "
                                       "without backslash or newline (e.g. a plainly named systemd service)")
    return path


def create_scope(*, isolate_cgroup=False) -> dict:
    """A fresh scope: the cutoff is taken BEFORE the random nonce exists, so no process that started earlier
    can have inherited it. Plain JSON; persist it durably before launch_owned and reuse it unchanged.
    isolate_cgroup=True also records the caller's cgroup (see check_cgroup_isolation)."""
    if type(isolate_cgroup) is not bool:
        raise OwnedProcessError("isolate_cgroup must be a boolean")
    _require_platform()
    cgroup = check_cgroup_isolation() if isolate_cgroup else None
    not_before = max(0, _uptime_ticks() - ROUNDING_TICKS)
    nonce = secrets.token_hex(16)
    scope = {"nonce": nonce, "bootId": _boot_id(), "notBeforeTicks": not_before}
    if cgroup is not None:
        scope["cgroup"] = cgroup
    _check_scope_schema(scope)
    return scope


# ------------------------------------------------------------------ /proc

def _exited(pidfd: int) -> bool:
    """A pidfd is readable once the whole process has exited (zombies included)."""
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return bool(poller.poll(0))


def _read_uid(pid: int) -> int:
    with open(f"/proc/{pid}/status", "rb") as handle:
        data = handle.read(1 << 16)
    for line in data.split(b"\n"):
        if line.startswith(b"Uid:"):
            try:
                return int(line.split()[1])   # real UID
            except (IndexError, ValueError):
                break
    raise ScanError(f"process {pid}: no readable Uid line in /proc status")


def _start_ticks(pid: int) -> int:
    """Field 22 of /proc/<pid>/stat: start time after boot, in clock ticks."""
    with open(f"/proc/{pid}/stat", "rb") as handle:
        data = handle.read(4096)
    try:
        return int(data.rsplit(b")", 1)[1].split()[19])
    except (IndexError, ValueError):
        raise ScanError(f"process {pid}: unexpected /proc stat format") from None


def _read_environ(pid: int) -> bytes:
    chunks, size = [], 0
    with open(f"/proc/{pid}/environ", "rb", buffering=0) as handle:
        while True:
            chunk = handle.read(1 << 16)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > MAX_ENVIRON_BYTES:
                raise ScanError(f"process {pid}: environment exceeds {MAX_ENVIRON_BYTES} bytes")
            chunks.append(chunk)


UNREADABLE_GRACE_SECONDS = 2.0


def _read_unreadable_patiently(pid: int, pidfd: int):
    """The environment, or None if the process exited. A PermissionError is retried for a short grace period:
    a process that is exiting or in the middle of an exec can be briefly unreadable. A process that stays alive
    and unreadable re-raises PermissionError (the caller fails closed)."""
    deadline = time.monotonic() + UNREADABLE_GRACE_SECONDS
    while True:
        try:
            return _read_environ(pid)
        except PermissionError:
            if _exited(pidfd):
                return None
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _describe(pid: int) -> str:
    """comm and exe of a process for diagnostics (both readable for same-UID processes); never raises."""
    parts = []
    try:
        with open(f"/proc/{pid}/comm", "rb") as handle:
            parts.append("comm=" + handle.read(64).decode("utf-8", "replace").strip())
    except OSError:
        parts.append("comm=?")
    try:
        parts.append("exe=" + os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        parts.append("exe=?")
    return " ".join(parts)


def _owned(pid: int, pidfd: int, entry: bytes, uid: int, not_before: int, cgroup=None) -> bool:
    """True if the process behind pidfd is alive, has this real UID and carries the exact nonce entry.

    Data read from /proc/<pid> belongs to the pidfd's process whenever that process is still alive after the
    reads: a PID cannot be reused before its process has been reaped. Every early False is safe either way."""
    try:
        if _read_uid(pid) != uid:
            return False
        if cgroup is not None and not _in_cgroup(_cgroup_path(pid), cgroup):
            return False
        if _start_ticks(pid) < not_before:
            return False   # started before the nonce existed, so it cannot have inherited it
        environ = _read_unreadable_patiently(pid, pidfd)
        if environ is None:
            return False
    except PermissionError:
        if _exited(pidfd):
            return False
        raise ScanError(f"process {pid} ({_describe(pid)}) belongs to this user, started within the scope and its "
                        f"environment is unreadable; ownership cannot be determined") from None
    except OSError as exc:
        if _exited(pidfd):
            return False
        raise ScanError(f"process {pid}: cannot read /proc ({exc.strerror or exc})") from None
    if _exited(pidfd):
        return False
    return entry in environ.split(b"\0")


def _scan(scope: dict) -> list:
    """[(pid, pidfd)] for live owned processes. The caller closes the pidfds. Raises ScanError if incomplete."""
    entry = f"{NONCE_ENV}={scope['nonce']}".encode("ascii")
    uid, me, not_before = os.getuid(), os.getpid(), scope["notBeforeTicks"]
    try:
        pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError as exc:
        raise ScanError(f"cannot list /proc ({exc.strerror})") from None
    owned = []
    try:
        for pid in pids:
            try:
                pidfd = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            except OSError as exc:
                raise ScanError(f"cannot open a pidfd for process {pid} ({exc.strerror})") from None
            keep = False
            try:
                if _owned(pid, pidfd, entry, uid, not_before, scope.get("cgroup")):
                    if pid == me:
                        raise OwnedProcessError("the calling process itself carries this nonce")
                    owned.append((pid, pidfd))
                    keep = True
            finally:
                if not keep:
                    os.close(pidfd)
    except BaseException:
        for _, pidfd in owned:
            os.close(pidfd)
        raise
    return owned


def _close(owned: list) -> None:
    for _, pidfd in owned:
        os.close(pidfd)


def _signal(pidfd: int, signum: int) -> bool:
    try:
        signal.pidfd_send_signal(pidfd, signum)
        return True
    except ProcessLookupError:
        return False   # exited meanwhile
    except OSError as exc:
        raise OwnedProcessError(f"cannot signal an owned process ({exc.strerror})") from None


# ------------------------------------------------------------------ API

def launch_owned(command, *, cwd, log, scope, env=None) -> subprocess.Popen:
    """Start command (no shell) in a new session with UIE_ATTEMPT_NONCE=<scope nonce> in its environment.

    scope comes from create_scope() and must already be persisted by the caller. log is an open regular file
    that is writable (stdout and stderr both go there) or subprocess.DEVNULL; pipes, FIFOs and sockets are
    refused to avoid deadlocks. env=None inherits os.environ. Everything is validated, and no live process
    may already carry the nonce, before anything is started. Returns the Popen, bound to this scope; stop it
    with stop_owned(scope, process=...)."""
    _check_scope_schema(scope)
    nonce = scope["nonce"]
    if not isinstance(command, (list, tuple)) or not command \
            or not all(type(arg) is str and "\0" not in arg for arg in command) or not command[0]:
        raise OwnedProcessError("command must be a non-empty list of strings without NUL (no shell)")
    if not isinstance(cwd, (str, os.PathLike)) or not os.path.isdir(cwd):
        raise OwnedProcessError("cwd must be an existing directory")
    base = dict(os.environ) if env is None else env
    if type(base) is not dict or not all(type(k) is str and type(v) is str and k and "=" not in k
                                         and "\0" not in k and "\0" not in v for k, v in base.items()):
        raise OwnedProcessError("env must map non-empty names without '=' or NUL to strings without NUL")
    if base.get(NONCE_ENV, nonce) != nonce:
        raise OwnedProcessError(f"the environment already carries a different {NONCE_ENV}")
    if log is not subprocess.DEVNULL and not callable(getattr(log, "fileno", None)):
        raise OwnedProcessError("log must be an open writable regular file or subprocess.DEVNULL")
    _require_platform()
    _check_scope_current(scope)
    if log is not subprocess.DEVNULL:
        import fcntl
        try:
            fd = log.fileno()
            info = os.fstat(fd)
            mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
        except (OSError, ValueError) as exc:
            raise OwnedProcessError(f"log is not an open file ({exc})") from None
        if not stat.S_ISREG(info.st_mode):
            raise OwnedProcessError("log must be a regular file, not a pipe, FIFO, socket or device")
        if mode not in (os.O_WRONLY, os.O_RDWR):
            raise OwnedProcessError("log is not open for writing")
    in_use = _scan(scope)
    _close(in_use)
    if in_use:
        raise OwnedProcessError("nonce is already carried by live processes; use a fresh scope per attempt")
    stamped = dict(base)
    stamped[NONCE_ENV] = nonce
    if "cgroup" in scope and not _in_cgroup(_cgroup_path(os.getpid()), scope["cgroup"]):
        raise OwnedProcessError("launching supervisor is outside the recorded cgroup")
    try:
        process = subprocess.Popen(list(command), cwd=os.fspath(cwd), env=stamped, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
                                   shell=False)
    except OSError as exc:
        raise OwnedProcessError(f"cannot start {command[0]!r} ({exc.strerror or type(exc).__name__})") from None
    _LAUNCHED[process] = dict(scope)
    return process


def inspect_owned(scope) -> list:
    """[{"pid": pid}] for live (non-zombie) processes of this real UID carrying the exact nonce entry."""
    _check_scope_schema(scope)
    _require_platform()
    _check_scope_current(scope)
    owned = _scan(scope)
    _close(owned)
    return [{"pid": pid} for pid, _ in sorted(owned)]


def stop_owned(scope, *, term_timeout=10.0, kill_timeout=10.0, process=None) -> dict:
    """Stop every live process carrying the scope's nonce: SIGTERM, then SIGKILL, re-scanning each round.

    Returns {"stopped": True, "live": [], "terminated", "killed", "escalated", "directChildWithoutNonce",
    "returncode"} only after a complete scan finds no live owned process. Raises CleanupError (with
    .remaining) if any survive, ScanError if ownership could not be determined, StaleScopeError for a scope of
    another boot. process, if given, must be the Popen launch_owned returned for this scope in this process;
    anything else is refused before any scan or signal. It is reaped, and killed if it lost the nonce."""
    _check_scope_schema(scope)
    term_timeout = _timeout(term_timeout, "term_timeout")
    kill_timeout = _timeout(kill_timeout, "kill_timeout")
    _check_process(process, scope)
    _require_platform()
    _check_scope_current(scope)
    terminated, killed = set(), set()
    escalated = False
    for signum, timeout in ((signal.SIGTERM, term_timeout), (signal.SIGKILL, kill_timeout)):
        deadline = time.monotonic() + timeout
        while True:
            if process is not None:
                process.poll()   # reap our direct child so it does not linger as a zombie
            owned = _scan(scope)
            try:
                for pid, pidfd in owned:
                    if signum == signal.SIGTERM and pid in terminated:
                        continue
                    if _signal(pidfd, signum):
                        (terminated if signum == signal.SIGTERM else killed).add(pid)
            finally:
                _close(owned)
            if not owned:
                break
            if time.monotonic() >= deadline:
                if signum == signal.SIGKILL:
                    raise CleanupError(f"{len(owned)} owned process(es) still alive after SIGKILL",
                                       sorted(pid for pid, _ in owned))
                escalated = True
                break
            time.sleep(POLL_INTERVAL)
        if not escalated:
            break

    without_nonce = False
    returncode = None
    if process is not None:
        if process.poll() is None:
            # Alive although no live process carries the nonce: it replaced its environment. It is still our
            # unreaped child, launched for this scope, so its PID cannot have been reused and Popen.kill is safe.
            without_nonce = True
            process.kill()
        try:
            returncode = process.wait(timeout=kill_timeout)
        except subprocess.TimeoutExpired:
            raise CleanupError(f"direct child {process.pid} did not exit after SIGKILL", [process.pid]) from None
    return {"stopped": True, "live": [], "terminated": sorted(terminated), "killed": sorted(killed),
            "escalated": escalated, "directChildWithoutNonce": without_nonce, "returncode": returncode}

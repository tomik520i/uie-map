"""Finite warm-plan worker coordinator (Linux, stdlib only). One call handles at most one queue job.

    with RenderQueue(db, world_id, dimension, pipeline_key, max_attempts=3) as queue:
        result = run_one(queue, attempts_dir, prepare=adapter)      # result["outcome"] tells what happened

One queue job is one whole finite warm plan, rendered by one capture-client launch. The prepare adapter
stages the plan and the runtime inside a fresh attempt directory and re-scans the runtime identity; this
module checks what the adapter returns, launches the client as an owned process tree (owned_process), waits
for every receipt, stops the tree, verifies the result independently (verify_rendered_plan) and only then
completes the queue job. No publication, no daemon, no live source access. See WARM-WORKER.md.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import signal
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import owned_process as op
import pipeline_identity
import verify_rendered_plan as vrp
from render_queue import QueueBusyError, QueueError, RenderQueue

STATE_FORMAT = "UIE_WARM_ATTEMPT1"
VERIFIED_FORMAT = "UIE_WARM_VERIFIED1"
STATE_FILE, VERIFIED_FILE, LOG_FILE, LOCK_FILE = "state.json", "VERIFIED.json", "client.log", "worker.lock"
QUEUE_LOCK_SUFFIX = ".worker-lock"   # sibling of the canonical SQLite file
MARKER = "PLAN-COMPLETE.json"
HEX64 = re.compile(r"[0-9a-f]{64}")
TOKEN = re.compile(r"[0-9a-f]{32}")
LIMIT_SMALL = 1 << 20
MAX_ERROR = 2000
PHASES = ("reserved", "prepared", "launching", "running", "stopped", "verified", "completed", "failed",
          "blocked", "abandoned")
LAUNCHED_PHASES = ("launching", "running", "stopped", "verified", "blocked")   # a scope must be recorded
STATE_KEYS = {"format", "jobId", "attempt", "token", "queue", "phase", "scope", "planDir", "planSha256", "error"}
SPEC_REQUIRED = {"planDir", "cwd", "command", "expectedPlanSha256", "currentIdentity"}
SPEC_OPTIONAL = {"env"}


class WarmWorkerError(Exception):
    """Invalid arguments or attempts directory; nothing was claimed."""


class WorkerBusyError(WarmWorkerError):
    """Another worker holds attempts_dir/worker.lock."""


class PermanentPrepareError(WarmWorkerError):
    """Raised by a prepare adapter for input that a retry cannot fix (the job is failed without retry)."""


class _Permanent(Exception):
    pass


class _Transient(Exception):
    pass


# ------------------------------------------------------------------ small helpers

def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _short(exc_or_text) -> str:
    text = exc_or_text if isinstance(exc_or_text, str) else f"{type(exc_or_text).__name__}: {exc_or_text}"
    return text if len(text) <= MAX_ERROR else text[:MAX_ERROR - 1] + "…"


def _number(value, name: str, low: float, high: float, low_inclusive=True) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) \
            or not (low <= value if low_inclusive else low < value) or value > high:
        raise WarmWorkerError(f"{name} must be a number in {'[' if low_inclusive else '('}{low:g}, {high:g}]")
    return float(value)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path: Path, doc) -> bytes:
    """Atomic, durable replace of path (temp file in the same directory, fsync, rename, fsync directory)."""
    raw = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(raw)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)
    _fsync_dir(path.parent)
    return raw


def _read_json(base: Path, relative: str):
    """Bounded, no-link JSON read (verify_rendered_plan's reader); RenderVerificationError on any problem."""
    return vrp._json(vrp._read(base, relative, LIMIT_SMALL), relative)


def _check_root(attempts_dir) -> Path:
    if not isinstance(attempts_dir, (str, os.PathLike)) or not os.path.isabs(attempts_dir):
        raise WarmWorkerError("attempts_dir must be an absolute path")
    root = Path(attempts_dir)
    try:
        for entry in (root, *root.parents):
            if stat.S_ISLNK(os.lstat(entry).st_mode):
                raise WarmWorkerError(f"{entry} is a symlink; attempts_dir needs a real path")
    except FileNotFoundError:
        raise WarmWorkerError(f"{root} does not exist") from None
    if not stat.S_ISDIR(os.lstat(root).st_mode) or os.path.realpath(root) != str(root):
        raise WarmWorkerError("attempts_dir must be a canonical existing directory")
    return root


def queue_lock_path(queue: RenderQueue) -> Path:
    """The worker lock of the queue's actual SQLite file (independent of any attempts root)."""
    database = queue.database_path()
    if database is None:
        raise WarmWorkerError("the queue must be a file database, not in-memory or temporary")
    try:
        info = os.lstat(database)
    except FileNotFoundError:
        raise WarmWorkerError(f"queue database {database} does not exist") from None
    if not stat.S_ISREG(info.st_mode):
        raise WarmWorkerError(f"queue database {database} is not a regular file")
    return Path(database + QUEUE_LOCK_SUFFIX)


@contextmanager
def _locked(path: Path):
    """Exclusive non-blocking flock on a regular lock file that is not a link."""
    import fcntl
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise WarmWorkerError(f"cannot open lock {path} ({exc.strerror}); links are refused") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WarmWorkerError(f"lock {path} is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkerBusyError(f"another worker holds {path}") from None
        yield
    finally:
        os.close(fd)   # releases the lock; O_CLOEXEC keeps it out of the client


@contextmanager
def _cancellation():
    """SIGINT/SIGTERM set a flag instead of raising (main thread only); the previous handlers are restored."""
    flag = threading.Event()
    if threading.current_thread() is not threading.main_thread():
        yield flag.is_set   # no signal handling off the main thread (documented)
        return
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        for signum in previous:
            signal.signal(signum, lambda *_: flag.set())
        yield flag.is_set
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler if handler is not None else signal.SIG_DFL)


# ------------------------------------------------------------------ attempt state

def _state(queue: RenderQueue, claim: dict) -> dict:
    return {"format": STATE_FORMAT, "jobId": claim["id"], "attempt": claim["attempt"], "token": claim["token"],
            "queue": {"worldId": queue.world_id, "dimension": queue.dimension, "pipelineKey": queue.pipeline_key},
            "phase": "reserved", "scope": None, "planDir": None, "planSha256": None, "error": None}


def _save(attempt_dir: Path, state: dict, **changes) -> None:
    state.update(changes)
    _write_json(attempt_dir / STATE_FILE, state)


def _load_state(attempt_dir: Path, queue: RenderQueue, job: dict, token: str) -> dict:
    state = _read_json(attempt_dir, STATE_FILE)
    ok = (type(state) is dict and set(state) == STATE_KEYS and state["format"] == STATE_FORMAT
          and state["jobId"] == job["id"] and type(state["attempt"]) is int and state["attempt"] == job["attempt"]
          and state["token"] == token
          and state["queue"] == {"worldId": queue.world_id, "dimension": queue.dimension,
                                 "pipelineKey": queue.pipeline_key}
          and state["phase"] in PHASES and state["phase"] != "completed"
          and (state["scope"] is None or type(state["scope"]) is dict)
          and (state["scope"] is not None or state["phase"] not in LAUNCHED_PHASES)
          and (state["error"] is None or type(state["error"]) is str))
    if not ok:
        raise vrp.RenderVerificationError("state.json does not describe this running job")
    return state


# ------------------------------------------------------------------ adapter result

def _inside(attempt_dir: Path, value, label: str) -> Path:
    """A real directory strictly inside attempt_dir, given as a canonical absolute path without links."""
    if type(value) is not str or "\0" in value or not os.path.isabs(value):
        raise _Permanent(f"{label} must be an absolute path string")
    path = Path(value)
    if os.path.realpath(path) != str(path) or str(path) != value.rstrip("/"):
        raise _Permanent(f"{label} must be canonical, without links")
    try:
        parts = path.relative_to(attempt_dir).parts
    except ValueError:
        raise _Permanent(f"{label} is outside the attempt directory") from None
    if not parts:
        raise _Permanent(f"{label} must be inside, not equal to, the attempt directory")
    current = attempt_dir
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            raise _Permanent(f"{label} does not exist") from None
        if not stat.S_ISDIR(info.st_mode):
            raise _Permanent(f"{label} is not a real directory")
    return path


def _validate_spec(spec, claim: dict, queue: RenderQueue, attempt_dir: Path) -> dict:
    if type(spec) is not dict or not SPEC_REQUIRED <= set(spec) <= SPEC_REQUIRED | SPEC_OPTIONAL:
        raise _Permanent(f"prepare must return {sorted(SPEC_REQUIRED)} (+ optional env)")
    plan_dir = _inside(attempt_dir, spec["planDir"], "planDir")
    cwd = _inside(attempt_dir, spec["cwd"], "cwd")
    command = spec["command"]
    if type(command) is not list or not command or not all(type(a) is str and a and "\0" not in a for a in command):
        raise _Permanent("command must be a non-empty list of non-empty strings (no shell)")
    env = spec.get("env")
    if env is not None and (type(env) is not dict or op.NONCE_ENV in env
                            or not all(type(k) is str and type(v) is str for k, v in env.items())):
        raise _Permanent(f"env must be a str->str dict without {op.NONCE_ENV}")
    expected = spec["expectedPlanSha256"]
    if type(expected) is not str or not HEX64.fullmatch(expected):
        raise _Permanent("expectedPlanSha256 must be 64 lowercase hex")
    if claim["payload"].get("planSha256") != expected:
        raise _Permanent("expectedPlanSha256 differs from the queue job's payload.planSha256")
    try:
        if os.path.lexists(plan_dir / "COMPLETE") and \
                vrp._read(plan_dir, "COMPLETE", LIMIT_SMALL) != vrp._read(plan_dir, MARKER, LIMIT_SMALL):
            raise _Permanent(f"COMPLETE and {MARKER} differ")
        plan_jobs, _, binding = vrp._verify_plan(plan_dir, expected, MARKER)
        jobs_doc = vrp._json(vrp._read(plan_dir, "jobs.json", vrp.LIMIT_JSON), "jobs.json")
        staged = None
        if binding["pipelineIdentity"] is not None:
            staged = pipeline_identity.validate_pipeline_identity(
                vrp._json(vrp._read(plan_dir, vrp.IDENTITY_FILE, vrp.LIMIT_JSON), vrp.IDENTITY_FILE))
        current = pipeline_identity.validate_pipeline_identity(spec["currentIdentity"])
    except (vrp.RenderVerificationError, pipeline_identity.PipelineIdentityError) as exc:
        raise _Permanent(f"staged plan or identity is invalid: {exc}") from None
    if binding["pipelineKeySource"] != "declared-inputs-sha256" or staged is None:
        raise _Permanent("the plan must bind a declared pipeline identity (declared-inputs-sha256)")
    if binding["pipelineKey"] != queue.pipeline_key:
        raise _Permanent("the plan's pipelineKey differs from the queue's")
    if jobs_doc.get("world") != queue.world_id or jobs_doc.get("dimension") != queue.dimension:
        raise _Permanent("the plan's world/dimension differ from the queue's")
    if current != staged:
        raise _Permanent("the freshly scanned runtime identity differs from the plan's pipeline-identity.json")
    for name in ("results", "worker-status.json"):
        if os.path.lexists(plan_dir / name):
            raise _Permanent(f"the staged plan is not fresh: {name} exists")
    return {"planDir": plan_dir, "cwd": cwd, "command": command, "env": env, "expected": expected,
            "jobs": plan_jobs}


# ------------------------------------------------------------------ run

def _monitor(process, plan_dir: Path, jobs: list, deadline: float, poll_interval: float, cancelled) -> None:
    """Return once every plan job has a captured receipt bound to its id and snapshot; raise otherwise."""
    while True:
        if cancelled():
            raise _Transient("cancelled by SIGINT/SIGTERM")
        try:
            if os.path.lexists(plan_dir / "worker-status.json"):
                status = _read_json(plan_dir, "worker-status.json")
                if type(status) is not dict or status.get("state") == "fatal":
                    raise _Transient("the capture client reported a fatal status")
            captured = 0
            for job in jobs:
                relative = f"results/{job['id']}/receipt.json"
                if not os.path.lexists(plan_dir / relative):
                    continue
                receipt = _read_json(plan_dir, relative)
                state = receipt.get("state") if type(receipt) is dict else None
                if state != "captured":
                    raise _Transient(f"job {job['id']}: receipt state {str(state)[:40]!r}")
                if receipt.get("id") != job["id"] or receipt.get("snapshotSha256") != job["snapshotSha256"] \
                        or type(receipt.get("manifestSha256")) is not str \
                        or not HEX64.fullmatch(receipt["manifestSha256"]):
                    raise _Transient(f"job {job['id']}: receipt is not bound to this job and snapshot")
                captured += 1
        except vrp.RenderVerificationError as exc:
            raise _Transient(f"unreadable status or receipt: {exc}") from None
        if captured == len(jobs):
            return
        if process.poll() is not None:
            raise _Transient(f"capture client exited ({process.returncode}) with {captured}/{len(jobs)} receipts")
        if time.monotonic() >= deadline:
            raise _Transient(f"timeout with {captured}/{len(jobs)} receipts")
        time.sleep(poll_interval)


def verify_attempt(plan_dir: Path, expected_plan_sha256: str, allow_unsupported: bool = False) -> dict:
    """Production verifier: verify_rendered_plan, strict about unsupported blocks unless allow_unsupported (then
    the report records unsupportedPolicy="allowed" and every later re-verification uses the same policy)."""
    return vrp.verify_rendered_plan(plan_dir, expected_plan_sha256=expected_plan_sha256, marker_name=MARKER,
                                    allow_unsupported=allow_unsupported)


def verify_attempt_allowing_unsupported(plan_dir: Path, expected_plan_sha256: str) -> dict:
    """verify_attempt with unsupported blocks accepted (still counted and named in the report)."""
    return verify_attempt(plan_dir, expected_plan_sha256, allow_unsupported=True)


def run_one(queue, attempts_dir, *, prepare, verify=verify_attempt, timeout=1200.0, poll_interval=0.2,
            retry_delay=60.0, term_timeout=10.0, kill_timeout=10.0, isolate_cgroup=False) -> dict:
    """Recover a running job left by a dead worker, or claim and run at most one job. See WARM-WORKER.md."""
    if not isinstance(queue, RenderQueue):
        raise WarmWorkerError("queue must be a RenderQueue")
    if not callable(prepare) or not callable(verify):
        raise WarmWorkerError("prepare and verify must be callables")
    if type(isolate_cgroup) is not bool:
        raise WarmWorkerError("isolate_cgroup must be a boolean")
    limits = {"timeout": _number(timeout, "timeout", 0, 86400, False),
              "poll": _number(poll_interval, "poll_interval", 0.01, 60),
              "retry": _number(retry_delay, "retry_delay", 0, 1e7),
              "term": _number(term_timeout, "term_timeout", 0, op.MAX_TIMEOUT),
              "kill": _number(kill_timeout, "kill_timeout", 0, op.MAX_TIMEOUT), "cgroup": isolate_cgroup}
    if not sys.platform.startswith("linux"):
        raise op.UnsupportedPlatformError("the warm worker needs Linux")
    root = _check_root(attempts_dir)
    # The queue-file lock first: the same database used with another attempts root is refused here, before
    # any recovery or claim. The attempts-root lock then covers several queues sharing one root.
    with _locked(queue_lock_path(queue)), _locked(root / LOCK_FILE), _cancellation() as cancelled:
        try:
            running = queue.running_claim()
        except QueueBusyError:
            raise
        except QueueError as exc:
            return {"outcome": "blocked", "error": _short(exc)}
        if running is not None:
            # recovery never needs isolation (stop_owned from anywhere)
            return _recover(queue, root, running, limits, verify)
        if limits["cgroup"]:
            # Probe before claiming: a host or service without usable cgroup v2 isolation must not consume
            # attempts or stage the runtime only to fail at create_scope().
            try:
                op.check_cgroup_isolation()
            except op.OwnedProcessError as exc:
                raise WarmWorkerError(f"isolate_cgroup=True but isolation is unavailable: {exc}") from None
        claim = queue.claim(time.time())
        if claim is None:
            return {"outcome": "idle"}
        return _attempt(queue, root, claim, prepare, verify, limits, cancelled)


def _attempt(queue, root: Path, claim: dict, prepare, verify, limits: dict, cancelled) -> dict:
    name = f"{claim['id']}-{claim['attempt']}-{claim['token']}"
    attempt_dir = root / name
    summary = {"jobId": claim["id"], "attempt": claim["attempt"], "attemptDir": str(attempt_dir)}
    state = _state(queue, claim)
    try:
        os.mkdir(attempt_dir, 0o700)   # exclusive: never reused
        _fsync_dir(root)
        _save(attempt_dir, state)
    except OSError as exc:
        # Without a durable state.json, recovery would block; failing now keeps the queue consistent.
        return _fail(queue, claim, None, None, summary, f"cannot reserve the attempt directory ({exc})", False,
                     limits)

    ctx = {"scope": None, "process": None, "log": None, "spec": None}
    error, permanent, stop = None, False, None
    try:
        try:
            _launch_and_monitor(ctx, queue, claim, attempt_dir, state, prepare, limits, cancelled)
        except _Permanent as exc:
            error, permanent = str(exc), True
        except Exception as exc:   # transient: crash, timeout, receipt error, adapter I/O, cancellation
            error = str(exc) if isinstance(exc, _Transient) else _short(exc)
    finally:
        cleanup_error = None
        if ctx["scope"] is not None:
            try:
                stop = op.stop_owned(ctx["scope"], term_timeout=limits["term"], kill_timeout=limits["kill"],
                                     process=ctx["process"])
            except Exception as exc:
                cleanup_error = exc
        if ctx["log"] is not None:
            ctx["log"].close()
        if cleanup_error is not None:
            # The tree may still be alive: never fail or requeue; the job stays running until recovery succeeds.
            _save(attempt_dir, state, phase="blocked", error=_short(f"cleanup failed: {_short(cleanup_error)}"))
    if cleanup_error is not None:
        return {"outcome": "blocked", **summary, "error": state["error"]}
    summary["stop"] = None if stop is None else {k: stop[k] for k in ("terminated", "killed", "escalated", "returncode")}
    if error is not None:
        return _fail(queue, claim, attempt_dir, state, summary, error, permanent, limits)

    _save(attempt_dir, state, phase="stopped")
    spec = ctx["spec"]
    try:
        report = verify(spec["planDir"], spec["expected"])
    except Exception as exc:
        return _fail(queue, claim, attempt_dir, state, summary, f"verification error: {_short(exc)}", False, limits)
    if type(report) is not dict or type(report.get("verified")) is not bool:
        return _fail(queue, claim, attempt_dir, state, summary, "verifier returned no report", True, limits)
    if not report["verified"]:
        reasons = "; ".join(str(r) for r in report.get("reasons", []))[:500]
        return _fail(queue, claim, attempt_dir, state, summary, f"not verified: {reasons}", True, limits)
    if report.get("planSha256") != spec["expected"] or report.get("pipelineKey") != queue.pipeline_key \
            or report.get("pipelineKeySource") != "declared-inputs-sha256":
        return _fail(queue, claim, attempt_dir, state, summary, "report is not bound to this plan and key", True,
                     limits)
    raw = _write_json(attempt_dir / VERIFIED_FILE, {
        "format": VERIFIED_FORMAT, "jobId": claim["id"], "attempt": claim["attempt"],
        "planSha256": spec["expected"], "pipelineKey": queue.pipeline_key, "report": report})
    _save(attempt_dir, state, phase="verified")
    result = {"attempt": name, "verifiedSha256": _sha(raw), "planSha256": spec["expected"],
              "pipelineKey": queue.pipeline_key}
    queue.complete(claim["id"], claim["token"], result, now=time.time())
    _save(attempt_dir, state, phase="completed")
    return {"outcome": "succeeded", **summary, "verifiedSha256": result["verifiedSha256"]}


def _launch_and_monitor(ctx, queue, claim, attempt_dir: Path, state: dict, prepare, limits, cancelled) -> None:
    try:
        spec = prepare({"id": claim["id"], "attempt": claim["attempt"], "payload": claim["payload"]}, attempt_dir)
    except PermanentPrepareError as exc:
        raise _Permanent(f"prepare: {_short(exc)}") from None
    checked = _validate_spec(spec, claim, queue, attempt_dir)
    ctx["spec"] = checked
    _save(attempt_dir, state, phase="prepared", planDir=str(checked["planDir"].relative_to(attempt_dir)),
          planSha256=checked["expected"])
    if cancelled():
        raise _Transient("cancelled by SIGINT/SIGTERM before launch")
    scope = op.create_scope(isolate_cgroup=limits["cgroup"])
    _save(attempt_dir, state, phase="launching", scope=scope)   # durable BEFORE anything starts
    ctx["scope"] = scope
    ctx["log"] = open(attempt_dir / LOG_FILE, "xb")
    ctx["process"] = op.launch_owned(checked["command"], cwd=checked["cwd"], log=ctx["log"], scope=scope,
                                     env=checked["env"])
    _save(attempt_dir, state, phase="running")
    _monitor(ctx["process"], checked["planDir"], checked["jobs"], time.monotonic() + limits["timeout"],
             limits["poll"], cancelled)


def _fail(queue, claim, attempt_dir, state, summary, error: str, permanent: bool, limits) -> dict:
    error = _short(error)
    if state is not None:
        _save(attempt_dir, state, phase="failed", error=error)
    result = queue.fail(claim["id"], claim["token"], error, time.time(), limits["retry"], retry=not permanent)
    return {"outcome": "retry" if result == "queued" else "failed", **summary, "permanent": permanent,
            "error": error}


REUSABLE_PHASES = ("stopped", "verified", "blocked")   # the client ran; its outputs may be complete


def _complete_recovered(queue, job: dict, attempt_dir: Path, state: dict, verify, summary: dict):
    """After a CONFIRMED cleanup: re-verify the attempt's outputs exactly like a normal success (fresh report,
    same plan/key binding, never an existing VERIFIED.json) and complete the job. None if not verifiable."""
    if state["phase"] not in REUSABLE_PHASES or state["planDir"] is None or state["planSha256"] is None:
        summary["reuseRefused"] = f"phase {state['phase']} has no complete outputs"
        return None
    try:
        # state.json stores planDir relative to the attempt; _inside checks the canonical, link-free location.
        plan_dir = _inside(attempt_dir, str(attempt_dir / state["planDir"]), "planDir")
        report = verify(plan_dir, state["planSha256"])
    except Exception as exc:
        summary["reuseRefused"] = _short(f"verification error: {_short(exc)}")
        return None
    if type(report) is not dict or report.get("verified") is not True or report.get("planSha256") != state["planSha256"] \
            or report.get("pipelineKey") != queue.pipeline_key or report.get("pipelineKeySource") != "declared-inputs-sha256":
        reasons = "; ".join(str(r) for r in report.get("reasons", []))[:300] if type(report) is dict else "no report"
        summary["reuseRefused"] = f"not verified or not bound to this plan and key: {reasons}"
        return None
    raw = _write_json(attempt_dir / VERIFIED_FILE, {
        "format": VERIFIED_FORMAT, "jobId": job["id"], "attempt": job["attempt"],
        "planSha256": state["planSha256"], "pipelineKey": queue.pipeline_key, "report": report})
    _save(attempt_dir, state, phase="verified", error=None)
    result = {"attempt": attempt_dir.name, "verifiedSha256": _sha(raw), "planSha256": state["planSha256"],
              "pipelineKey": queue.pipeline_key}
    queue.complete(job["id"], job["token"], result, now=time.time())
    _save(attempt_dir, state, phase="completed")
    return {"outcome": "succeeded", **summary, "recoveredFromPhase": summary["previousPhase"],
            "verifiedSha256": result["verifiedSha256"]}


def _recover(queue, root: Path, job: dict, limits: dict, verify=None) -> dict:
    """Stop the recorded scope of the running claim's exact attempt, then requeue it. Block on any doubt.

    Exception: if the attempt reached a post-launch phase (stopped/verified/blocked) and, after the cleanup was
    confirmed, a fresh strict verification of its outputs passes with the same binding as a normal success, the
    job is completed from those outputs instead of being re-rendered.

    job is queue.running_claim(): the attempt directory name and its state.json must carry the LIVE claim
    token. Anything else (an older claim's attempt, a state whose token differs) blocks before any signal.
    The live token only appears where the attempt layout already stores it (directory name, state.json)."""
    summary = {"jobId": job["id"], "attempt": job["attempt"]}
    pattern = re.compile(re.escape(f"{job['id']}-{job['attempt']}-") + r"([0-9a-f]{32})")
    matches = [name for name in os.listdir(root) if pattern.fullmatch(name)]
    if len(matches) != 1:
        return {"outcome": "blocked", **summary,
                "error": f"{len(matches)} attempt directories match the running job; manual recovery needed"}
    token = pattern.fullmatch(matches[0]).group(1)
    if type(job["token"]) is not str or not secrets.compare_digest(token, job["token"]):
        return {"outcome": "blocked", **summary,
                "error": "the attempt directory does not belong to the live claim; manual recovery needed"}
    attempt_dir = root / matches[0]
    summary["attemptDir"] = str(attempt_dir)
    if not stat.S_ISDIR(os.lstat(attempt_dir).st_mode):
        return {"outcome": "blocked", **summary, "error": "the attempt entry is not a real directory"}
    try:
        state = _load_state(attempt_dir, queue, job, job["token"])   # state token must equal the live token
    except vrp.RenderVerificationError as exc:
        return {"outcome": "blocked", **summary, "error": _short(f"invalid attempt state: {exc}")}
    stop = None
    if state["scope"] is not None:
        try:
            stop = op.stop_owned(state["scope"], term_timeout=limits["term"], kill_timeout=limits["kill"])
        except op.StaleScopeError:
            stop = {"staleBoot": True}   # recorded on another boot: none of its processes can still exist
        except Exception as exc:
            _save(attempt_dir, state, phase="blocked", error=_short(f"recovery cleanup failed: {_short(exc)}"))
            return {"outcome": "blocked", **summary, "error": state["error"]}
    if verify is not None:
        summary["previousPhase"] = state["phase"]
        completed = _complete_recovered(queue, job, attempt_dir, state, verify, summary)
        if completed is not None:
            return completed
        del summary["previousPhase"]
    _save(attempt_dir, state, phase="abandoned", error=_short(f"worker stopped in phase {state['phase']}; "
                                                              f"recovered without reusing results"))
    recovered = queue.recover_running(time.time(), 0.0)
    return {"outcome": "recovered", **summary, "queueState": recovered[0]["state"] if recovered else None,
            "stop": None if stop is None else {k: stop[k] for k in stop if k in
                                               ("terminated", "killed", "escalated", "staleBoot")}}

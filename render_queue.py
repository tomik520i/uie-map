"""Durable single-worker render job queue on SQLite (standard library only).

A queue file belongs to exactly one identity: world_id, dimension, pipeline_key and max_attempts. Opening
it with any other identity raises IdentityMismatchError; it never switches silently.

    with RenderQueue(path, world_id, dimension, pipeline_key, max_attempts=3) as q:
        q.enqueue([{"id": <64 lowercase hex fingerprint>, "payload": {...JSON object...}}, ...])
        job = q.claim(now)              # {"id", "payload", "attempt", "token"} or None
        q.complete(job["id"], job["token"], {...caller-verified artifact info...}, now=...)
        q.fail(job["id"], job["token"], "error text", now, retry_delay=60.0)   # -> "queued" | "failed"
        q.recover_running(now)          # only when no worker is alive (see below)
        q.stats(); q.list_jobs(state=None)
        q.database_path()               # canonical main database file, None for in-memory/temporary
        q.running_claim()               # {"id", "attempt", "token", "payload"} of the running job, or None

Semantics
- enqueue is one transaction: all jobs are added or none. An id that already exists with the same canonical
  payload (sorted keys, no whitespace) is left untouched in whatever state it is (queued, running,
  succeeded, failed); a different payload for an existing id rejects the whole call (ConflictError).
  enqueue never removes or resets jobs, so the failed ledger survives an empty enqueue, a re-enqueue of the
  same inventory and a restart. Requeueing failed jobs is not implemented yet.
- claim(now) returns nothing while any job is running (at most one running job per queue file). Otherwise
  it takes the queued job with the lowest insertion order whose due time is <= now, increments attempt and
  returns a fresh random token.
- complete/fail require the job's current running token; any other token (e.g. from before
  recover_running) raises StaleClaimError. complete stores the caller's result object as-is: the queue does
  not verify geometry or artifacts. fail requeues with due = now + retry_delay, or marks the job failed once
   attempt >= max_attempts, or when retry=False explicitly marks a permanent failure. Error text is truncated
   to MAX_ERROR_CHARS. The default retry=True preserves existing behaviour.
- recover_running(now, retry_delay=0) handles a running job left by a crashed worker: it is requeued, or
  failed if its attempts are exhausted, and its token becomes invalid. claim never steals a running job;
  the operator/driver must guarantee that only one worker process uses a queue file and must call
  recover_running only when that worker is known to be gone.
- Times are caller-supplied finite numbers (e.g. epoch seconds); the queue does not read the clock except
  as a default completion time. Retry scheduling therefore follows the caller's clock.
- Every operation runs in a BEGIN IMMEDIATE transaction on one connection; a second instance on the same
  file waits up to busy_timeout seconds and then raises QueueBusyError.

- database_path() reports the file SQLite actually opened (PRAGMA database_list), resolved with realpath, so
  a supervisor can lock the queue itself rather than a path alias. It is None for :memory: or temporary
  databases. running_claim() is a narrow read for crash recovery: it returns the running job's current
  claim token so a supervisor can match it against its own durable attempt record. The token authorizes
  complete/fail; list_jobs and stats never include it, and callers must not log or publish it. More than
  one running row (impossible through this API) raises QueueError instead of picking one.

Not included: inventory advancement, publication, removal of deleted tiles, worker status reporting.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = "1"
MAX_ERROR_CHARS = 2000
MAX_DOCUMENT_BYTES = 1 << 20
_SHA = re.compile(r"[0-9a-f]{64}")
_STATES = ("queued", "running", "succeeded", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'failed')),
    attempt INTEGER NOT NULL DEFAULT 0,
    due REAL,
    token TEXT,
    claimed_at REAL,
    finished_at REAL,
    result TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_by_state ON jobs (state, seq);
"""


class QueueError(Exception):
    """Base class for render queue errors."""


class IdentityMismatchError(QueueError):
    """The queue file belongs to another world, dimension, pipeline or configuration."""


class ConflictError(QueueError):
    """An existing job id was enqueued with a different payload."""


class StaleClaimError(QueueError):
    """The job is not running under the given token."""


class QueueBusyError(QueueError):
    """Another connection holds the queue's write lock."""


def _text(value, label: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty string of at most {limit} characters")
    return value


def _time(value, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _canonical(value, label: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not plain JSON: {exc}") from None
    if json.loads(text) != value:
        raise ValueError(f"{label} contains non-JSON types (tuples, non-string keys, ...)")
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"{label} exceeds {MAX_DOCUMENT_BYTES} bytes")
    return text


class RenderQueue:
    def __init__(self, db_path, world_id: str, dimension: str, pipeline_key: str, *, max_attempts: int = 3,
                 busy_timeout: float = 5.0) -> None:
        self.world_id = _text(world_id, "world_id")
        self.dimension = _text(dimension, "dimension")
        if not isinstance(pipeline_key, str) or not _SHA.fullmatch(pipeline_key):
            raise ValueError("pipeline_key must be 64 lowercase hex digits")
        self.pipeline_key = pipeline_key
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 1000:
            raise ValueError("max_attempts must be an integer 1..1000")
        self.max_attempts = max_attempts
        timeout = _time(busy_timeout, "busy_timeout")
        self._conn = sqlite3.connect(str(Path(db_path)), timeout=max(0.0, timeout), isolation_level=None)
        try:
            self._conn.execute("PRAGMA synchronous = FULL")
            self._initialize()
        except BaseException:
            self._conn.close()
            self._conn = None
            raise

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> RenderQueue:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self):
        if self._conn is None:
            raise QueueError("queue is closed")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise QueueBusyError(f"queue is locked by another connection: {exc}") from None
            raise
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _initialize(self) -> None:
        identity = {"schema": SCHEMA_VERSION, "world_id": self.world_id, "dimension": self.dimension,
                    "pipeline_key": self.pipeline_key, "max_attempts": str(self.max_attempts)}
        with self._tx() as db:
            for statement in _SCHEMA.strip().split(";"):
                if statement.strip():
                    db.execute(statement)
            stored = dict(db.execute("SELECT key, value FROM meta").fetchall())
            if not stored:
                if db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]:
                    raise IdentityMismatchError("queue file has jobs but no identity")
                db.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", identity.items())
            elif stored != identity:
                differing = sorted(k for k in identity.keys() | stored.keys() if identity.get(k) != stored.get(k))
                raise IdentityMismatchError(f"queue file identity differs in {', '.join(differing)}")

    # ------------------------------------------------------------ operations

    def enqueue(self, jobs) -> dict:
        """Add jobs atomically; returns {"added": n, "existing": m}."""
        if not isinstance(jobs, (list, tuple)):
            raise ValueError("jobs must be a list")
        prepared: dict[str, str] = {}
        for i, job in enumerate(jobs):
            if not isinstance(job, dict) or set(job) != {"id", "payload"}:
                raise ValueError(f"jobs[{i}] must be {{'id', 'payload'}}")
            job_id = job["id"]
            if not isinstance(job_id, str) or not _SHA.fullmatch(job_id):
                raise ValueError(f"jobs[{i}].id must be 64 lowercase hex digits")
            payload = _canonical(job["payload"], f"jobs[{i}].payload")
            if prepared.get(job_id, payload) != payload:
                raise ConflictError(f"job {job_id} appears twice with different payloads")
            prepared[job_id] = payload
        added = existing = 0
        with self._tx() as db:
            for job_id, payload in prepared.items():
                row = db.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    db.execute("INSERT INTO jobs (id, payload, state) VALUES (?, ?, 'queued')", (job_id, payload))
                    added += 1
                elif row[0] == payload:
                    existing += 1
                else:
                    raise ConflictError(f"job {job_id} already exists with a different payload")
        return {"added": added, "existing": existing}

    def claim(self, now) -> dict | None:
        now = _time(now, "now")
        with self._tx() as db:
            if db.execute("SELECT 1 FROM jobs WHERE state = 'running' LIMIT 1").fetchone():
                return None
            row = db.execute("SELECT seq, id, payload, attempt FROM jobs WHERE state = 'queued' "
                             "AND (due IS NULL OR due <= ?) ORDER BY seq LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            seq, job_id, payload, attempt = row
            token = secrets.token_hex(16)
            db.execute("UPDATE jobs SET state = 'running', attempt = ?, token = ?, claimed_at = ?, due = NULL "
                       "WHERE seq = ?", (attempt + 1, token, now, seq))
        return {"id": job_id, "payload": json.loads(payload), "attempt": attempt + 1, "token": token}

    def _running(self, db, job_id, token):
        if not isinstance(job_id, str) or not isinstance(token, str):
            raise ValueError("job id and token must be strings")
        row = db.execute("SELECT seq, attempt FROM jobs WHERE id = ? AND state = 'running' AND token = ?",
                         (job_id, token)).fetchone()
        if row is None:
            raise StaleClaimError(f"job {job_id} is not running under this token")
        return row

    def complete(self, job_id: str, token: str, result: dict, now=None) -> None:
        text = _canonical(result, "result")
        finished = time.time() if now is None else _time(now, "now")
        with self._tx() as db:
            seq, _ = self._running(db, job_id, token)
            db.execute("UPDATE jobs SET state = 'succeeded', token = NULL, result = ?, finished_at = ? WHERE seq = ?",
                       (text, finished, seq))

    def fail(self, job_id: str, token: str, error: str, now, retry_delay=60.0, *, retry=True) -> str:
        """Record failure; retry=False permanently fails this claim without exhausting remaining attempts."""
        if type(retry) is not bool:
            raise ValueError("retry must be a boolean")
        if not isinstance(error, str):
            raise ValueError("error must be a string")
        now = _time(now, "now")
        delay = _time(retry_delay, "retry_delay")
        if delay < 0:
            raise ValueError("retry_delay must be >= 0")
        _time(now + delay, "retry due time")
        error = error if len(error) <= MAX_ERROR_CHARS else error[:MAX_ERROR_CHARS - 1] + "…"
        with self._tx() as db:
            seq, attempt = self._running(db, job_id, token)
            if not retry or attempt >= self.max_attempts:
                db.execute("UPDATE jobs SET state = 'failed', token = NULL, error = ?, finished_at = ? WHERE seq = ?",
                           (error, now, seq))
                return "failed"
            db.execute("UPDATE jobs SET state = 'queued', token = NULL, error = ?, due = ? WHERE seq = ?",
                       (error, now + delay, seq))
            return "queued"

    def recover_running(self, now, retry_delay=0.0) -> list[dict]:
        """Requeue or finally fail interrupted running jobs. Call only when no worker is alive."""
        now = _time(now, "now")
        delay = _time(retry_delay, "retry_delay")
        if delay < 0:
            raise ValueError("retry_delay must be >= 0")
        _time(now + delay, "retry due time")
        recovered = []
        with self._tx() as db:
            for seq, job_id, attempt in db.execute(
                    "SELECT seq, id, attempt FROM jobs WHERE state = 'running' ORDER BY seq").fetchall():
                message = f"interrupted: worker stopped during attempt {attempt}"
                if attempt >= self.max_attempts:
                    db.execute("UPDATE jobs SET state = 'failed', token = NULL, error = ?, finished_at = ? "
                               "WHERE seq = ?", (message, now, seq))
                    recovered.append({"id": job_id, "state": "failed"})
                else:
                    db.execute("UPDATE jobs SET state = 'queued', token = NULL, error = ?, due = ? WHERE seq = ?",
                               (message, now + delay, seq))
                    recovered.append({"id": job_id, "state": "queued"})
        return recovered

    # ------------------------------------------------------------ inspection

    def stats(self) -> dict:
        with self._tx() as db:
            counts = dict(db.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall())
        stats = {state: counts.get(state, 0) for state in _STATES}
        stats["total"] = sum(stats.values())
        return stats

    def list_jobs(self, state: str | None = None) -> list[dict]:
        if state is not None and state not in _STATES:
            raise ValueError(f"state must be one of {_STATES}")
        query = ("SELECT seq, id, payload, state, attempt, due, claimed_at, finished_at, result, error FROM jobs"
                 + (" WHERE state = ?" if state else "") + " ORDER BY seq")
        with self._tx() as db:
            rows = db.execute(query, (state,) if state else ()).fetchall()
        return [{"seq": seq, "id": job_id, "payload": json.loads(payload), "state": st, "attempt": attempt,
                 "due": due, "claimedAt": claimed, "finishedAt": finished,
                 "result": None if result is None else json.loads(result), "error": error}
                for seq, job_id, payload, st, attempt, due, claimed, finished, result, error in rows]

    def database_path(self) -> str | None:
        """Canonical path of the main database file SQLite opened; None for in-memory or temporary databases."""
        if self._conn is None:
            raise QueueError("queue is closed")
        for _, name, file in self._conn.execute("PRAGMA database_list").fetchall():
            if name == "main":
                return os.path.realpath(file) if file else None
        return None

    def running_claim(self) -> dict | None:
        """The running job's current claim {"id", "attempt", "token", "payload"}, or None.

        Read-only, for supervisor crash recovery only. The token authorizes complete/fail: do not log it."""
        with self._tx() as db:
            rows = db.execute("SELECT id, attempt, token, payload FROM jobs WHERE state = 'running' "
                              "ORDER BY seq").fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise QueueError(f"{len(rows)} running jobs; the queue allows at most one")
        job_id, attempt, token, payload = rows[0]
        return {"id": job_id, "attempt": attempt, "token": token, "payload": json.loads(payload)}

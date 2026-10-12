"""Bounded one-shot render of a SEPARATE surface model candidate store (no service, no publication).

    python3 model_candidate_run.py --out OWNED_STORE --world WORLD --catalog-dir CATALOG [--resume]
                                   [--reserve-bytes N (default 104 GiB)] [--max-seconds 1..604800 (default 7 days)]

Linux only. Runs the sibling world_hires.py of THIS code directory once, for the whole world (default one worker,
--workers permits 1..4 within the separately configured service memory limit):
    world_hires.py --world W --catalog-dir C --out OUT --workers 1 --lock-wait-seconds 60 --prepare-immutable
(no --caves/--cave-caps, no --max-jobs; bounded immutable preparation supplies the receipt for later publication).

Guards:
- Runner lock OUT/.model-candidate-run.lock: exclusive, non-blocking POSIX flock (O_NOFOLLOW, regular file, name and
  descriptor identity) held for the WHOLE operation; a second coordinator fails before reading or writing anything
  else. It is separate from the renderer's .build.lock.
- Fresh start: OUT must be owned (hires_bundle marker), absolute, symlink-free and otherwise EMPTY: only the marker,
  this exact lock and this runner's status/log of a start that was refused before launching anything.
- --resume (explicit, never implied): the previous runner status must bind the SAME resolved world, catalog, OUT,
  sibling code identity (SHA-256 of offline_mesher.py, offline_catalog.py, offline_volume.py next to this file) and
  the same renderer command; OUT has no current.json and no cave-data/, its index.json (if any) is not a cave index,
  and no process group of the previous renderer is alive. --allow-worker-change with --resume permits ONLY changing
  the worker count of canonical commands; all output bindings remain identical and previousCommand is recorded.
  world_hires then continues with its own incremental job
  fingerprints; nothing is deleted. These bindings are written to the status before the first launch.
- Start: shutil.disk_usage(OUT).free must be >= reserve, else state "refused" without starting anything.
- The renderer runs in its own process group (session). Each wait is bounded by min(poll, time left until the
  monotonic deadline); every poll samples free disk. Below the reserve or at the deadline the whole group gets
  SIGTERM, then SIGKILL, and is awaited (cave_batch._kill_group). SIGTERM/SIGINT of the runner does the same.
  stdout/stderr go to a bounded log (1 MiB, cave_batch._drain).
- Result from the renderer's own status.json (fresh: started after launch), never from the exit code alone:
    completed   exit 0, state done, planComplete true, notScheduled 0, failedJobs 0, failedTiles 0, group gone
    incomplete  exit 0 and done, but failed jobs/tiles or an incomplete plan
    failed      non-zero exit, no fresh status, a renderer state other than done, or renderer-group-alive (members
                of the renderer's process group survived cleanup)
    stopped     low-disk / deadline / interrupted (group killed)
    refused     low disk at start (nothing started)
Status: OUT/model-candidate-run.json (UIE_MODEL_CANDIDATE_RUN1), atomically rewritten at start, every poll and at
the end. CLI exit 0 only for "completed", 1 otherwise, 2 for invalid arguments or platform.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

import cave_batch
import hires_bundle as hb

FORMAT = 'UIE_MODEL_CANDIDATE_RUN1'
STATUS_NAME = 'model-candidate-run.json'
LOG_NAME = 'model-candidate-run.log'
LOCK_NAME = '.model-candidate-run.lock'
RESERVE_BYTES = 104 * 1024 ** 3
MAX_SECONDS = 7 * 86400
POLL_SECONDS = 30
KILL_GRACE_SECONDS = 10
LOCK_WAIT_SECONDS = 60
MAX_RENDER_STATUS_BYTES = 1024 * 1024
MAX_RUNNER_STATUS_BYTES = 256 * 1024
WORLD_HIRES = Path(__file__).with_name('world_hires.py')
OUTPUT_MODULES = ('offline_mesher.py', 'offline_catalog.py', 'offline_volume.py')
ALLOWED_AT_START = {hb.OWNED_MARKER, LOCK_NAME, STATUS_NAME, LOG_NAME}


class CandidateError(ValueError):
    pass


class Interrupted(BaseException):
    pass


def renderer_command(out, world, catalog_dir, workers=1):
    return [sys.executable, str(WORLD_HIRES), '--world', str(world), '--catalog-dir', str(catalog_dir),
            '--out', str(out), '--workers', str(workers), '--lock-wait-seconds', str(LOCK_WAIT_SECONDS), '--prepare-immutable']


def code_identity():
    """SHA-256 over the output-defining sibling modules (names and bytes); what world_hires hashes into identity."""
    digest = hashlib.sha256()
    for name in OUTPUT_MODULES:
        raw = WORLD_HIRES.with_name(name).read_bytes()
        digest.update(name.encode() + b'\0' + hashlib.sha256(raw).digest())
    return digest.hexdigest()


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                     # exists, owned by someone else: never treat as gone


def acquire_lock(out):
    """Descriptor holding LOCK_EX on OUT/.model-candidate-run.lock; CandidateError when another runner holds it."""
    import fcntl
    fd = os.open(out / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CandidateError('candidate runner lock is not a regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CandidateError('another candidate runner holds the lock') from None
        named, opened = os.stat(out / LOCK_NAME, follow_symlinks=False), os.fstat(fd)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise CandidateError('candidate runner lock replaced')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _owned(out):
    try:
        return hb._owned(Path(out))
    except FileNotFoundError:
        raise CandidateError('candidate out or its ownership marker is missing') from None


def _previous_status(out):
    try:
        raw, _ = hb._read(out / STATUS_NAME, MAX_RUNNER_STATUS_BYTES)     # lstat-regular: never a symlink
    except FileNotFoundError:
        return None
    doc = hb._strict_json(raw, 'candidate runner status')
    if not isinstance(doc, dict) or doc.get('format') != FORMAT:
        raise CandidateError('previous candidate runner status has an unknown format')
    return doc


def check_fresh(out, previous):
    """Otherwise empty OUT; only a refused (never launched) start of this runner may have left its status/log."""
    extra = sorted(name for name in os.listdir(out) if name not in ALLOWED_AT_START)
    if extra:
        raise CandidateError(f'candidate out is not empty: {extra[:5]} (use --resume only for an own interrupted run)')
    for name in (STATUS_NAME, LOG_NAME):
        if os.path.lexists(out / name):
            hb._regular(out / name)
    if previous is not None and previous.get('state') != 'refused':
        raise CandidateError('candidate out already has a launched run: use --resume')


def worker_change_only(old, new):
    """Explicit scheduling adjustment: only the worker value of canonical renderer commands may differ."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if {k:v for k,v in old.items() if k!='command'} != {k:v for k,v in new.items() if k!='command'}:
        return False
    for binding in (old, new):
        command = binding.get('command')
        if not isinstance(command, list) or command.count('--workers') != 1:
            return False
        i = command.index('--workers')
        if i+1 >= len(command) or command[i+1] not in ('1','2','3','4'):
            return False
        expected = renderer_command(binding.get('out'), binding.get('world'), binding.get('catalog'), int(command[i+1]))
        if command != expected:
            return False
    return True


def check_resume(out, previous, bindings, *, allow_worker_change=False):
    if previous is None:
        raise CandidateError('nothing to resume: no previous candidate runner status')
    if previous.get('bindings') != bindings and not (allow_worker_change and worker_change_only(previous.get('bindings'), bindings)):
        raise CandidateError('previous candidate run has different world/catalog/out/code/command bindings')
    if os.path.lexists(out / 'current.json'):
        raise CandidateError('candidate out has a published current.json; refusing to resume')
    if os.path.lexists(out / 'cave-data'):
        raise CandidateError('candidate out contains cave-data; refusing to resume')
    if os.path.lexists(out / 'index.json'):
        raw, _ = hb._read(out / 'index.json', 16 * 1024 * 1024)
        index = hb._strict_json(raw, 'candidate legacy index')
        if not isinstance(index, dict) or index.get('caves') is not False:
            raise CandidateError('candidate out index.json is not a surface index')
    pid = previous.get('rendererPid')
    if type(pid) is int and pid > 0 and _group_alive(pid):
        raise CandidateError('a process group of the previous renderer is still alive')


def _summary(status):
    if not isinstance(status, dict):
        return None
    pick = lambda key: status.get(key) if type(status.get(key)) is int else None
    return {'state': status.get('state')[:300] if isinstance(status.get('state'), str) else None,
            'planComplete': status.get('planComplete') if type(status.get('planComplete')) is bool else None,
            **{key: pick(key) for key in ('total', 'done', 'plannedJobs', 'notScheduled', 'failedJobs', 'failedTiles')},
            **{key: status.get(key) if type(status.get(key)) in (int, float) else None
               for key in ('startedAt', 'updatedAt', 'finishedAt')}}


def classify(returncode, renderer, launched, group_gone=True):
    """(state, reason) of a renderer that exited on its own."""
    if not group_gone:
        return 'failed', 'renderer-group-alive'            # survivors may still write: never "completed"
    if returncode != 0:
        return 'failed', f'renderer-exit-{returncode}'
    if not renderer or renderer['startedAt'] is None or renderer['startedAt'] < launched:
        return 'failed', 'renderer-no-fresh-status'        # e.g. "skipped: another build holds the lock"
    if renderer['state'] != 'done':
        return 'failed', 'renderer-stopped'
    if renderer['failedJobs'] != 0 or renderer['failedTiles'] != 0:
        return 'incomplete', 'render-failed'
    if renderer['planComplete'] is not True or renderer['notScheduled'] != 0:
        return 'incomplete', 'plan-incomplete'
    return 'completed', None


def run(out, world, catalog_dir, *, resume=False, workers=1, allow_worker_change=False, reserve_bytes=RESERVE_BYTES, max_seconds=MAX_SECONDS,
        poll_seconds=POLL_SECONDS, kill_grace=KILL_GRACE_SECONDS, command=None, disk_usage=shutil.disk_usage,
        clock=time.time, monotonic=time.monotonic):
    if not sys.platform.startswith('linux'):
        raise RuntimeError('candidate runner requires Linux (process groups)')
    if type(resume) is not bool:
        raise ValueError('resume must be boolean')
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError('workers must be 1..4')
    if type(allow_worker_change) is not bool or (allow_worker_change and not resume):
        raise ValueError('allow_worker_change requires an explicit resume')
    if type(reserve_bytes) is not int or reserve_bytes < 0:
        raise ValueError('reserve_bytes must be a nonnegative integer')
    for name, value, high in (('max_seconds', max_seconds, MAX_SECONDS), ('poll_seconds', poll_seconds, 3600)):
        if type(value) not in (int, float) or not 0 < value <= high:
            raise ValueError(f'{name} must be in (0, {high}]')
    out = _owned(out)
    world, catalog_dir = Path(world).resolve(), Path(catalog_dir).resolve()
    if out == world or world in out.parents:
        raise CandidateError('candidate out must lie outside the world')
    command = list(command or renderer_command(out, world, catalog_dir, workers))
    bindings = {'out': str(out), 'world': str(world), 'catalog': str(catalog_dir), 'codeSha256': code_identity(),
                'worldHires': str(WORLD_HIRES), 'command': command}
    lock = acquire_lock(out)
    try:
        previous = _previous_status(out)
        if resume:
            check_resume(out, previous, bindings, allow_worker_change=allow_worker_change)
        else:
            check_fresh(out, previous)
        return _run_locked(out, bindings, previous if resume else None, reserve_bytes=reserve_bytes,
                           max_seconds=max_seconds, poll_seconds=poll_seconds, kill_grace=kill_grace,
                           disk_usage=disk_usage, clock=clock, monotonic=monotonic)
    finally:
        os.close(lock)


def _run_locked(out, bindings, previous, *, reserve_bytes, max_seconds, poll_seconds, kill_grace, disk_usage,
                clock, monotonic):
    command = bindings['command']
    started, deadline = clock(), monotonic() + max_seconds
    doc = {'format': FORMAT, 'state': 'starting', 'reason': None, 'startedAt': started, 'updatedAt': started,
           'finishedAt': None, 'reserveBytes': reserve_bytes, 'maxSeconds': max_seconds, 'freeBytes': None,
           'rendererPid': None, 'returncode': None, 'groupGone': None, 'leftoverGroup': None, 'renderer': None,
           'previousCommand': previous.get('bindings',{}).get('command') if previous else None,
           'bindings': bindings, 'attempt': (previous.get('attempt', 1) if type(previous.get('attempt')) is int else 1) + 1
           if previous else 1, 'resumedFromStartedAt': previous.get('startedAt') if previous else None}

    def save(**fields):
        doc.update(fields, updatedAt=clock())
        hb._atomic(out / STATUS_NAME, json.dumps(doc, sort_keys=True).encode())

    def finish(state, reason=None, **fields):
        save(state=state, reason=reason, finishedAt=clock(), **fields)
        return dict(doc)

    free = disk_usage(out).free
    if free < reserve_bytes:
        return finish('refused', 'low-disk-at-start', freeBytes=free)
    save(freeBytes=free)                                  # bindings are durable before the first launch
    log = os.open(out / LOG_NAME, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    launched = clock()
    try:
        process = subprocess.Popen(command, cwd=str(WORLD_HIRES.parent), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                                   close_fds=True)
    except BaseException:
        os.close(log)
        raise
    reader = threading.Thread(target=cave_batch._drain, args=(process.stdout, log), daemon=True)
    reader.start()

    def interrupt(signum, frame):
        raise Interrupted(signal.Signals(signum).name)
    previous_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, interrupt)
    try:
        save(state='running', rendererPid=process.pid)
        stop_reason = None
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                stop_reason = 'deadline'
            else:
                try:
                    returncode = process.wait(timeout=min(poll_seconds, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
                free = disk_usage(out).free
                if free < reserve_bytes:
                    stop_reason = 'low-disk'
            if stop_reason:
                gone = cave_batch._kill_group(process, grace=kill_grace)
                return finish('stopped', stop_reason, freeBytes=free, returncode=process.returncode, groupGone=gone)
            save(freeBytes=free)
        # The renderer exited on its own; anything left in its group (stray workers holding the log pipe) is stopped
        # before the log reader is joined, and recorded. Survivors make the run "failed", never "completed".
        if _group_alive(process.pid):
            leftover, gone = True, cave_batch._kill_group(process, grace=kill_grace)
        else:
            leftover, gone = False, True
    except BaseException as error:
        gone = cave_batch._kill_group(process, grace=kill_grace)
        try:
            finish('stopped', 'interrupted' if isinstance(error, (Interrupted, KeyboardInterrupt)) else
                   f'exception: {type(error).__name__}: {error}'[:300], returncode=process.returncode, groupGone=gone)
        except Exception:
            pass
        raise
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        reader.join(timeout=kill_grace + 5)
    renderer = None
    try:
        raw, _ = hb._read(out / 'status.json', MAX_RENDER_STATUS_BYTES)
        renderer = _summary(hb._strict_json(raw, 'renderer status'))
    except (OSError, ValueError):
        renderer = None
    state, reason = classify(returncode, renderer, launched, gone)
    return finish(state, reason, returncode=returncode, groupGone=gone, leftoverGroup=leftover, renderer=renderer,
                  freeBytes=disk_usage(out).free)


def _bounded(name, low, high):
    def parse(text):
        try:
            value = int(text, 10)
        except ValueError:
            raise argparse.ArgumentTypeError(f'{name} must be an integer') from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f'{name} must be {low}..{high}')
        return value
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--world', required=True, type=Path)
    parser.add_argument('--catalog-dir', required=True, type=Path)
    parser.add_argument('--resume', action='store_true', help='continue an own interrupted run with identical bindings')
    parser.add_argument('--workers', type=_bounded('workers', 1, 4), default=1)
    parser.add_argument('--allow-worker-change', action='store_true', help='with --resume, allow only a canonical worker-count change')
    parser.add_argument('--reserve-bytes', type=_bounded('reserve-bytes', 0, 2**62), default=RESERVE_BYTES)
    parser.add_argument('--max-seconds', type=_bounded('max-seconds', 1, MAX_SECONDS), default=MAX_SECONDS)
    args = parser.parse_args(argv)
    if not sys.platform.startswith('linux'):
        print('model_candidate_run: Linux only', file=sys.stderr)
        return 2
    try:
        result = run(args.out, args.world, args.catalog_dir, resume=args.resume, workers=args.workers,
                     allow_worker_change=args.allow_worker_change, reserve_bytes=args.reserve_bytes,
                     max_seconds=args.max_seconds)
    except (OSError, ValueError, RuntimeError, Interrupted) as error:
        print(f'model_candidate_run: {type(error).__name__}: {error}'.replace('\n', ' ')[:500], file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result['state'] == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())

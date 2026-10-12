"""Private bounded local cave batch: 1..10 consecutive one-region renders, each followed by publication.

    python3 cave_batch.py --world W --catalog-dir C --out STORE --world-id ID --dimension-id DIM
                          [--max-steps 1..10] [--step-timeout 1..900] [--center X,Z]
                          [--max-verify-ticks 1..512 (default 8)] [--publish-deadline 1..900 s (default 300)]

Linux only. No service, timer, deployment, GC or deletion. Explicit invocation only.

Each step:
1. Renderer subprocess `world_hires.py --workers 1 --max-jobs 1 --fair-schedule --caves --cave-caps
   --prepare-immutable --lock-wait-seconds 60` (default budget/reserve, own process group, timeout <= 900 s; on
   timeout the whole group is killed and awaited). stdout+stderr go to a bounded log
   cave-batch-logs/step-<n>.log (<= 1 MiB per step, overwritten by the next batch).
2. The renderer's status.json must be fresh (started after launch), state done, exactly one job done, no failed
   jobs/tiles, immutable preparation prepared|pending. status.idle (nothing-pending, disk-reserve, backoff) stops
   the batch as 'idle' without further renders.
3. In-process publisher: hires_publisher.run_once(cave_pairs=True) with ONE PublishSession for the whole batch
   (verification cache kept across steps). Per step: unfinished 'verifying' warmup ticks have their own explicit
   limit (--max-verify-ticks, default 8; a final successful tick is allowed after exactly that many), unfinished
   preparing / producer-busy / waiting-for-producer ticks share the separate limit of 8, and the whole publish
   phase has a monotonic deadline (--publish-deadline, default 300 s) checked BEFORE each tick; a running tick is
   never pre-empted, the service runtime limit (systemd) is the outer bound. Limits are never raised
   automatically. Every other state, an exception, or a published geometry whose capPublication is not published
   stops the batch. A step succeeds only when geometry AND caps are published. lastPublisher reports ticks,
   verifyTicks and retryTicks of the current step; reasons publisher-verify-limit / publisher-incomplete /
   publisher-deadline name the exhausted bound.

The batch holds its own POSIX lock out/.cave-batch-lock (O_NOFOLLOW, regular file, name/descriptor identity) for
its whole duration. It never holds out/.build.lock across a subprocess: the renderer and run_once take it
themselves. At start the store must be owned, have cave-data/, and its current immutable index must have context
caves true (no new store, never a surface store).

Summary out/cave-batch-status.json (UIE_CAVE_BATCH_STATUS1, atomic): state running|completed|idle|error, reason,
completedSteps, maxSteps, step, phase, startedAt, updatedAt, elapsedSeconds, lastRenderer, lastPublisher.
It is rewritten before every phase, and an error status is written even when an exception escapes.
Exit: 0 completed/idle, 1 error, 3 another batch holds the batch lock, 2 invalid arguments or platform.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time

import hires_bundle as hb


FORMAT = 'UIE_CAVE_BATCH_STATUS1'
STATUS_NAME = 'cave-batch-status.json'
LOCK_NAME = '.cave-batch-lock'
LOG_DIR = 'cave-batch-logs'
MAX_STEPS = 10
MAX_STEP_TIMEOUT = 900
MAX_PUBLISH_TICKS = 8
MAX_VERIFY_TICKS = 8                  # default: unchanged pilot behaviour; raise only explicitly (--max-verify-ticks)
MAX_VERIFY_TICKS_LIMIT = 512
PUBLISH_DEADLINE_SECONDS = 300
MAX_PUBLISH_DEADLINE = 900
MAX_LOG_BYTES = 1024 * 1024
MAX_RENDER_STATUS_BYTES = 1024 * 1024
RENDER_LOCK_WAIT_SECONDS = 60
KILL_GRACE_SECONDS = 10
RETRY_STATES = ('preparing', 'verifying', 'producer-busy', 'waiting-for-producer')
RETRY_SLEEP_SECONDS = 2
WORLD_HIRES = Path(__file__).with_name('world_hires.py')


class BatchError(Exception):
    pass


class BatchInterrupted(BaseException):
    pass


def _terminate(signum, frame):
    raise BatchInterrupted('SIGTERM')


# ------------------------------------------------------------------ small helpers

def _text(value, limit=300):
    return value[:limit] if isinstance(value, str) else None


def _int(value):
    return value if type(value) is int else None


def _renderer_summary(status):
    """Whitelisted fields of world_hires status.json (never the error list)."""
    if not isinstance(status, dict):
        return None
    preparation = status.get('immutablePreparation')
    schedule = status.get('schedule') if isinstance(status.get('schedule'), dict) else {}
    selected = schedule.get('selected') if isinstance(schedule.get('selected'), dict) else {}
    return {'state': _text(status.get('state')), 'total': _int(status.get('total')), 'done': _int(status.get('done')),
            'failedJobs': _int(status.get('failedJobs')), 'failedTiles': _int(status.get('failedTiles')),
            'notScheduled': _int(status.get('notScheduled')), 'skippedUnchanged': _int(status.get('skippedUnchanged')),
            'planComplete': status.get('planComplete') if type(status.get('planComplete')) is bool else None,
            'idle': _text(status.get('idle'), 40), 'startedAt': status.get('startedAt')
            if type(status.get('startedAt')) in (int, float) else None,
            'selected': {k: _int(selected.get(k)) for k in ('coverage', 'update')},
            'preparation': _text(preparation.get('state'), 40) if isinstance(preparation, dict) else None}


def _publisher_summary(record, ticks, *, verify_ticks=0, retry_ticks=0):
    cap = record.get('capPublication') if isinstance(record.get('capPublication'), dict) else None
    return {'state': _text(record.get('state'), 80), 'published': record.get('published') is True,
            'indexSha256': _text(record.get('indexSha256'), 64), 'unchanged': record.get('unchanged') is True,
            'ticks': ticks, 'verifyTicks': verify_ticks, 'retryTicks': retry_ticks,
            'error': _text(record.get('error'), 500),
            'capPublication': None if cap is None else {
                'state': _text(cap.get('state'), 80), 'published': cap.get('published') is True,
                'indexSha256': _text(cap.get('indexSha256'), 64), 'missing': _int(cap.get('missing')),
                'stale': _int(cap.get('stale')), 'remaining': _int(cap.get('remaining')),
                'error': _text(cap.get('error'), 500)}}


def _write_status(out, doc):
    hb._atomic(out / STATUS_NAME, json.dumps(doc, sort_keys=True, separators=(',', ':')).encode())


def check_store(out):
    """Owned existing cave store with a published immutable cave index; never creates anything."""
    out = hb._owned(out)
    hb._real_dir(out / 'cave-data')
    current = hb._current_index(out)
    if current is None:
        raise BatchError('store has no published immutable index (refusing to start a new store)')
    if not current[2] or current[2].get('caves') is not True:
        raise BatchError('current immutable index is not cave geometry')
    return out


def acquire_batch_lock(out):
    """Descriptor holding LOCK_EX on out/.cave-batch-lock, or None when another batch holds it."""
    import fcntl
    fd = os.open(out / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BatchError('batch lock is not a regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        named, opened = os.stat(out / LOCK_NAME, follow_symlinks=False), os.fstat(fd)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise BatchError('batch lock replaced')
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ renderer subprocess

def _log_file(out, step):
    folder = out / LOG_DIR
    folder.mkdir(exist_ok=True)
    if not stat.S_ISDIR(os.lstat(folder).st_mode):
        raise BatchError('batch log folder is not a real directory')
    return os.open(folder / f'step-{step}.log', os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)


def _drain(pipe, fd, limit=MAX_LOG_BYTES):
    """Copy at most `limit` bytes to fd, then keep draining (never blocks the child on a full pipe)."""
    written = 0
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            if written < limit:
                part = chunk[:limit - written]
                os.write(fd, part)
                written += len(part)
                if written >= limit:
                    os.write(fd, b'\n[cave_batch: log truncated]\n')
    finally:
        pipe.close()
        os.close(fd)


def _kill_group(process, *, grace=KILL_GRACE_SECONDS, sleep=time.sleep):
    """SIGTERM then SIGKILL the child's whole process group; wait for the child and (bounded) for the group."""
    group = process.pid                               # start_new_session: the child leads its own group
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(group, signal.SIGKILL)              # also pool workers that outlive or ignore the parent
    except ProcessLookupError:
        pass
    process.wait()
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return True
        sleep(0.1)
    return False


def renderer_command(out, world, catalog_dir, center=None):
    command = [sys.executable, str(WORLD_HIRES), '--world', str(world), '--catalog-dir', str(catalog_dir),
               '--out', str(out), '--workers', '1', '--max-jobs', '1', '--fair-schedule', '--caves', '--cave-caps',
               '--prepare-immutable', '--lock-wait-seconds', str(RENDER_LOCK_WAIT_SECONDS)]
    if center is not None:
        command += ['--center', f'{center[0]},{center[1]}']
    return command


def render_subprocess(out, step, command, *, timeout, clock=time.time, monotonic=time.monotonic):
    """Run one renderer process; return a summary built from its fresh status.json (or None when stale/missing)."""
    launched, started = clock(), monotonic()
    log = _log_file(out, step)
    try:
        process = subprocess.Popen(command, cwd=str(WORLD_HIRES.parent), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                                   close_fds=True)
    except BaseException:
        os.close(log)
        raise
    reader = threading.Thread(target=_drain, args=(process.stdout, log), daemon=True)
    reader.start()
    timed_out, group_gone = False, True
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        group_gone = _kill_group(process)
        returncode = process.returncode
    except BaseException:
        _kill_group(process)                      # interrupted batch: never leave a renderer behind
        raise
    finally:
        reader.join(timeout=KILL_GRACE_SECONDS + 5)
    status = None
    try:
        raw, _ = hb._read(out / 'status.json', MAX_RENDER_STATUS_BYTES)
        status = hb._strict_json(raw, 'renderer status')
    except (OSError, ValueError):
        status = None
    summary = _renderer_summary(status)
    fresh = bool(summary and summary['startedAt'] is not None and summary['startedAt'] >= launched)
    return {'returncode': returncode, 'timedOut': timed_out, 'groupGone': group_gone,
            'seconds': round(monotonic() - started, 3), 'fresh': fresh, 'status': summary if fresh else None,
            'log': f'{LOG_DIR}/step-{step}.log'}


# ------------------------------------------------------------------ controller (no locks, injectable)

def render_outcome(result):
    """('ok'|'idle'|'error', reason) for a render result."""
    if result.get('timedOut'):
        return 'error', 'renderer-timeout' + ('' if result.get('groupGone', True) else '-group-alive')
    if result.get('returncode') != 0:
        return 'error', f"renderer-exit-{result.get('returncode')}"
    status = result.get('status')
    if not result.get('fresh') or not status:
        return 'error', 'renderer-no-fresh-status'        # e.g. build lock busy: renderer skipped
    if status['state'] != 'done':
        return 'error', 'renderer-stopped'
    if status['idle'] is not None:
        return 'idle', status['idle']
    if status['failedJobs'] != 0 or status['failedTiles'] != 0:
        return 'error', 'render-failed'
    if status['total'] != 1 or status['done'] != 1:
        return 'error', 'render-incomplete'
    if status['preparation'] not in ('prepared', 'pending'):
        return 'error', f"preparation-{status['preparation']}"
    return 'ok', None


def _validate_publish_limits(max_verify_ticks, publish_deadline):
    if type(max_verify_ticks) is not int or not 1 <= max_verify_ticks <= MAX_VERIFY_TICKS_LIMIT:
        raise ValueError(f'max_verify_ticks must be 1..{MAX_VERIFY_TICKS_LIMIT}')
    if (type(publish_deadline) not in (int, float) or not math.isfinite(publish_deadline)
            or not 1 <= publish_deadline <= MAX_PUBLISH_DEADLINE):
        raise ValueError(f'publish_deadline must be 1..{MAX_PUBLISH_DEADLINE} seconds')


def controller(out, *, max_steps, render, publish, session, max_publish_ticks=MAX_PUBLISH_TICKS,
               max_verify_ticks=MAX_VERIFY_TICKS, publish_deadline=PUBLISH_DEADLINE_SECONDS,
               clock=time.time, monotonic=time.monotonic, sleep=time.sleep, write=_write_status):
    """Run the steps; returns the final status document. render(step) -> result; publish(session) -> record.

    Per step's publish phase (counters reset for every step; the PublishSession/cache is shared by the batch):
    - verifyTicks: unfinished 'verifying' ticks. A further tick starts while verifyTicks <= max_verify_ticks, so a
      final successful tick is still allowed after exactly max_verify_ticks unfinished ones; one more unfinished
      verifying tick stops the batch with 'publisher-verify-limit'.
    - retryTicks: unfinished preparing / producer-busy / waiting-for-producer ticks. A further tick starts only
      while retryTicks < max_publish_ticks (unchanged meaning: at most that many such ticks), else
      'publisher-incomplete'. Neither counter resets the other.
    - publish_deadline: monotonic seconds since the phase began, checked BEFORE every tick ('publisher-deadline').
      A tick already running is never pre-empted; the outer bound is the service runtime limit (systemd).
    """
    if type(max_steps) is not int or not 1 <= max_steps <= MAX_STEPS:
        raise ValueError(f'max_steps must be 1..{MAX_STEPS}')
    if type(max_publish_ticks) is not int or not 1 <= max_publish_ticks <= MAX_PUBLISH_TICKS:
        raise ValueError(f'max_publish_ticks must be 1..{MAX_PUBLISH_TICKS}')
    _validate_publish_limits(max_verify_ticks, publish_deadline)
    started = clock()
    doc = {'format': FORMAT, 'state': 'running', 'reason': None, 'completedSteps': 0, 'maxSteps': max_steps,
           'step': None, 'phase': None, 'startedAt': started, 'updatedAt': started, 'elapsedSeconds': 0.0,
           'lastRenderer': None, 'lastPublisher': None}

    def save(**changes):
        doc.update(changes)
        now = clock()
        doc['updatedAt'] = now
        doc['elapsedSeconds'] = round(max(0.0, now - started), 3)
        write(out, doc)

    def finish(state, reason=None):
        save(state=state, reason=reason, phase=None)
        return dict(doc)

    try:
        save()
        for step in range(1, max_steps + 1):
            save(step=step, phase='render')
            result = render(step)
            save(lastRenderer=result)
            kind, reason = render_outcome(result)
            if kind != 'ok':
                return finish(kind, reason)
            save(phase='publish')
            phase_started = monotonic()
            ticks = verify_ticks = retry_ticks = 0
            outcome = None
            while outcome is None:
                if monotonic() - phase_started >= publish_deadline:
                    outcome = 'publisher-deadline'              # checked between ticks only
                    break
                ticks += 1
                try:
                    record = publish(session)
                except (OSError, ValueError) as error:
                    record = {'state': 'error', 'published': False, 'error': f'{type(error).__name__}: {error}'}
                if record.get('published') is not True:
                    if record.get('state') == 'verifying':
                        verify_ticks += 1
                    elif record.get('state') in RETRY_STATES:
                        retry_ticks += 1
                summary = _publisher_summary(record, ticks, verify_ticks=verify_ticks, retry_ticks=retry_ticks)
                save(lastPublisher=summary)
                if summary['published']:
                    cap = summary['capPublication']
                    outcome = 'ok' if cap and cap['published'] else f"cap-{cap['state'] if cap else 'missing'}"
                elif summary['state'] not in RETRY_STATES:
                    outcome = f"publisher-{summary['state']}"
                elif verify_ticks > max_verify_ticks:
                    outcome = 'publisher-verify-limit'
                elif retry_ticks >= max_publish_ticks:
                    outcome = 'publisher-incomplete'
                elif summary['state'] not in ('preparing', 'verifying'):
                    sleep(RETRY_SLEEP_SECONDS)                  # busy / waiting: back off before the next tick
            if outcome != 'ok':
                return finish('error', outcome)
            save(completedSteps=step)
        return finish('completed')
    except BaseException as error:
        try:
            save(state='error', reason=f'exception: {type(error).__name__}: {error}'[:500], phase=None)
        except Exception:
            pass
        raise


# ------------------------------------------------------------------ entry point

def run_batch(out, *, world, catalog_dir, world_id, dimension_id, max_steps=1, step_timeout=MAX_STEP_TIMEOUT,
              center=None, render=None, publish=None, session=None, sleep=time.sleep,
              max_verify_ticks=MAX_VERIFY_TICKS, publish_deadline=PUBLISH_DEADLINE_SECONDS):
    if not sys.platform.startswith('linux'):
        raise RuntimeError('cave batch requires Linux (POSIX flock, process groups)')
    _validate_publish_limits(max_verify_ticks, publish_deadline)
    if type(step_timeout) is not int or not 1 <= step_timeout <= MAX_STEP_TIMEOUT:
        raise ValueError(f'step_timeout must be 1..{MAX_STEP_TIMEOUT} seconds')
    if type(max_steps) is not int or not 1 <= max_steps <= MAX_STEPS:
        raise ValueError(f'max_steps must be 1..{MAX_STEPS}')
    from hires_context import validate_context
    validate_context(dict(world=world_id, dimension=dimension_id, renderer='check', caves=True, layoutSha256='0' * 64))
    out = check_store(Path(out))
    world, catalog_dir = Path(world).resolve(), Path(catalog_dir).resolve()
    fd = acquire_batch_lock(out)
    if fd is None:
        return {'state': 'busy', 'reason': 'another cave batch holds the batch lock'}
    try:
        check_store(out)                                   # re-check under the batch lock
        context=hb._current_index(out)[2]
        if context['world']!=world_id or context['dimension']!=dimension_id:
            raise BatchError('world/dimension differs from the existing cave store')
        if render is None:
            command = renderer_command(out, world, catalog_dir, center)
            render = lambda step: render_subprocess(out, step, command, timeout=step_timeout)
        if publish is None:
            from hires_publisher import run_once
            publish = lambda current: run_once(out, current, world=world_id, dimension=dimension_id, cave_pairs=True)
        if session is None:
            from hires_publish_session import PublishSession
            session = PublishSession()
        return controller(out, max_steps=max_steps, render=render, publish=publish, session=session, sleep=sleep,
                          max_verify_ticks=max_verify_ticks, publish_deadline=publish_deadline)
    finally:
        os.close(fd)


def _center(text):
    try:
        x, z = (int(v, 10) for v in text.split(','))
    except ValueError:
        raise argparse.ArgumentTypeError('center must be X,Z integers') from None
    if not all(-30_000_000 <= v <= 30_000_000 for v in (x, z)):
        raise argparse.ArgumentTypeError('center outside the world border')
    return x, z


def _bounded(name, high):
    def parse(text):
        try:
            value = int(text, 10)
        except ValueError:
            raise argparse.ArgumentTypeError(f'{name} must be an integer') from None
        if not 1 <= value <= high:
            raise argparse.ArgumentTypeError(f'{name} must be 1..{high}')
        return value
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--world', required=True, type=Path)
    parser.add_argument('--catalog-dir', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--world-id', required=True)
    parser.add_argument('--dimension-id', required=True)
    parser.add_argument('--max-steps', type=_bounded('max-steps', MAX_STEPS), default=1)
    parser.add_argument('--step-timeout', type=_bounded('step-timeout', MAX_STEP_TIMEOUT), default=MAX_STEP_TIMEOUT)
    parser.add_argument('--center', type=_center)
    parser.add_argument('--max-verify-ticks', type=_bounded('max-verify-ticks', MAX_VERIFY_TICKS_LIMIT),
                        default=MAX_VERIFY_TICKS, help='unfinished verification warmup ticks tolerated per step '
                        f'(default {MAX_VERIFY_TICKS}); raise explicitly for a larger store')
    parser.add_argument('--publish-deadline', type=_bounded('publish-deadline', MAX_PUBLISH_DEADLINE),
                        default=PUBLISH_DEADLINE_SECONDS, help='seconds per step publish phase, checked between '
                        f'ticks, not inside a tick (default {PUBLISH_DEADLINE_SECONDS})')
    args = parser.parse_args(argv)
    if not sys.platform.startswith('linux'):
        print('cave_batch: Linux only', file=sys.stderr)
        return 2
    previous_term=signal.signal(signal.SIGTERM,_terminate)
    try:
        result = run_batch(args.out, world=args.world, catalog_dir=args.catalog_dir, world_id=args.world_id,
                           dimension_id=args.dimension_id, max_steps=args.max_steps, step_timeout=args.step_timeout,
                           center=args.center, max_verify_ticks=args.max_verify_ticks,
                           publish_deadline=args.publish_deadline)
    except (OSError, ValueError, BatchError, RuntimeError, BatchInterrupted) as error:
        print(f'cave_batch: {type(error).__name__}: {error}'.replace('\n', ' ')[:500], file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM,previous_term)
    print(json.dumps(result, sort_keys=True))
    return {'completed': 0, 'idle': 0, 'busy': 3}.get(result['state'], 1)


if __name__ == '__main__':
    sys.exit(main())

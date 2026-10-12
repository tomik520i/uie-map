"""Private opt-in publisher runner. No service installation, deletion or renderer launch.

One process preserves verification proofs across bounded ticks. Each tick acquires
the producer's exclusive lock non-blockingly and releases it before waiting.
Invoke only against an explicitly owned store with the opt-in producer receipt.

--write-status (default off) additionally replaces <out>/publisher-status.json after every
tick with a bounded whitelisted snapshot (hires_publisher_status: state, counts, time, index
hash; never error text). It is written outside the build lock, after the tick's result is
final; a failed write is only logged and never repeats or changes the tick.
"""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import stat
import sys
import threading
import time

import hires_bundle as hb
from hires_context import validate_context
from hires_publish_session import PublishSession
from hires_publish_tick import tick


def run_once(out, session, *, world, dimension, cave_pairs=False):
    if type(cave_pairs) is not bool:raise ValueError('cave_pairs must be boolean')
    if os.name != 'posix':
        raise RuntimeError('publisher requires POSIX flock')
    import fcntl
    out = hb._owned(out)
    fd = os.open(out / '.build.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise hb.BundleError('build lock is not regular')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state': 'producer-busy', 'published': False}
        # Refuse replacement of the lock name after opening it.
        named = os.stat(out / '.build.lock', follow_symlinks=False)
        opened = os.fstat(fd)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise hb.BundleError('build lock replaced')
        return tick(out, session, world=world, dimension=dimension,
                    **({'allow_initial_partial_caves':True,'publish_cave_pairs':True} if cave_pairs else {}))
    finally:
        os.close(fd)


def _cached_tiles(session):
    """Verification proofs currently held by the session's process-local cache (internal field).

    NOT the number of tiles in the published map: it counts objects verified for the current
    references and kept in memory, grows during warmup, and drops to 0 after a process restart."""
    proofs = getattr(getattr(session, 'cache', None), '_proofs', None)
    return len(proofs) if isinstance(proofs, dict) else None


def _peak_rss_kib():
    """Peak resident set size of this process in KiB, or None when it cannot be measured.

    Linux only: there ru_maxrss is KiB (other systems use other units). Never reports 0 as a value."""
    if not sys.platform.startswith('linux'):
        return None
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, OSError, ValueError, AttributeError):
        return None
    return value if type(value) is int and value > 0 else None


def _telemetry(session, started):
    """Per-tick fields appended to the emitted record; the timer covers the tick work only, not the wait."""
    return {'elapsedSeconds': round(max(0.0, time.monotonic() - started), 3),
            'verificationCachedTiles': _cached_tiles(session), 'peakRssKiB': _peak_rss_kib()}


def run(out, *, world, dimension, interval=10, max_ticks=1, stop=None, emit=None, write_status=False,cave_pairs=False):
    """Default one tick; max_ticks=0 explicitly requests an interruptible loop.

    Operational errors emit an error record and continue after the same interval.
    No error replaces the last good map pointer. Logs go to the caller, not to an
    unbounded disk journal. Stop is checked between ticks; an active tick finishes.
    Every record (also errors) carries elapsedSeconds (monotonic tick work, without the
    wait), verificationCachedTiles (in-memory proofs, not published tiles) and peakRssKiB
    (Linux ru_maxrss, null when unknown). No extra process or request. The only extra file is
    publisher-status.json, and only with write_status=True: a whitelisted public snapshot
    (see hires_publisher_status). A failed status write adds statusWriteError to the emitted
    (private) record; the tick is neither repeated nor reported differently, and the previous
    status file stays until the next successful write (readers treat an old updatedAt as stale).
    """
    if (type(interval) not in (int, float) or not math.isfinite(interval)
            or interval < 1 or interval > 3600):
        raise ValueError('interval must be 1..3600 seconds')
    if type(max_ticks) is not int or max_ticks < 0:
        raise ValueError('max_ticks must be a nonnegative integer')
    if type(write_status) is not bool:
        raise ValueError('write_status must be boolean')
    if type(cave_pairs) is not bool:raise ValueError('cave_pairs must be boolean')
    validate_context(dict(world=world, dimension=dimension, renderer='check',
                          caves=False, layoutSha256='0' * 64))
    out = hb._owned(Path(out))
    if os.name != 'posix':
        raise RuntimeError('publisher requires POSIX flock')
    stop = stop if stop is not None else threading.Event()
    emit = emit if emit is not None else lambda record: print(json.dumps(record), flush=True)
    session = PublishSession()
    count = 0
    while not stop.is_set() and (not max_ticks or count < max_ticks):
        count += 1
        started = time.monotonic()
        try:
            record = run_once(out, session, world=world, dimension=dimension,cave_pairs=cave_pairs)
        except (OSError, ValueError, hb.BundleError) as error:
            record = {'state': 'error', 'published': False,
                      'error': f'{type(error).__name__}: {error}'[:2000]}
        record = dict(record, tick=count, **_telemetry(session, started))
        if write_status:
            from hires_publisher_status import write_status as save_status
            try:
                save_status(out, record)
            except Exception as error:        # any failure: never crash (a restart loses the warm cache)
                # Publication already has its own result: a failed status write must
                # neither retry the tick nor misreport a committed map as uncommitted.
                record['statusWriteError'] = f'{type(error).__name__}: {error}'[:2000]
        emit(record)
        if not max_ticks or count < max_ticks:
            stop.wait(interval)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--world', required=True)
    parser.add_argument('--dimension', required=True)
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('--max-ticks', type=int, default=1)
    parser.add_argument('--cave-pairs',action='store_true',help='Explicit cave-only publication including paired caps; permits first truthful partial cave index. Never use on surface store.')
    parser.add_argument('--write-status', action='store_true',
                        help='after every tick atomically replace <out>/publisher-status.json with a bounded, '
                             'whitelisted public snapshot (state, counts, time, index hash; no error text); '
                             'default off')
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    run(args.out, world=args.world, dimension=args.dimension, interval=args.interval,
        max_ticks=args.max_ticks, stop=stop, write_status=args.write_status,cave_pairs=args.cave_pairs)


if __name__ == '__main__':
    main()

"""Private, bounded immutable-hires GC executor; no scheduler or service.

Every call acquires the producer's exclusive build lock, creates a fresh retention
plan, and accepts no caller-supplied candidates. Dry run is the default. Explicit
execution unlinks only checked content-addressed names under the owned store.

CLI (exactly one collection per invocation, JSON result on stdout):
    python3 hires_gc.py --out STORE [--execute] [--max-files 1..128] [--max-bytes 1..67108864]
Exit 0 with a result (also state "producer-busy"), 1 on an error (one line on stderr), 2 on invalid arguments.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

import hires_bundle as hb
import hires_retention as retention


MAX_FILES = 128
MAX_BYTES = 64 * 1024 * 1024


def _candidate(out, item):
    relative = item.get('path') if isinstance(item, dict) else None
    if not isinstance(relative, str):
        raise hb.BundleError('invalid retention candidate path')
    parts = relative.split('/')
    if len(parts) != 2 or parts[0] not in retention._KINDS:
        raise hb.BundleError('candidate outside managed immutable directories')
    folder, name = parts
    suffix, limit = retention._KINDS[folder]
    if not retention._named(name, suffix):
        raise hb.BundleError('candidate is not a managed content-addressed name')
    identity = item.get('identity')
    if (not isinstance(identity, dict)
            or set(identity) != {'device', 'inode', 'size', 'mtimeNs', 'ctimeNs'}
            or any(type(value) is not int or value < 0 for value in identity.values())
            or not 1 <= identity['size'] <= limit):
        raise hb.BundleError('invalid retention candidate identity')
    hb._real_dir(out / folder)
    return out / folder / name, identity


def _same(path, expected):
    info = hb._regular(path)
    if retention._identity(info) != expected:
        raise hb.BundleError(f'candidate changed since retention scan: {path.name}')


def collect(out, *, execute=False, max_files=MAX_FILES, max_bytes=MAX_BYTES, now=None):
    """Plan and optionally unlink up to 128 files / 64 MiB under a fresh lock.

    `max_bytes` is a logical sum of candidate file sizes, not physical disk savings.
    A busy producer returns without planning. Any changed candidate refuses before
    its unlink; a failure after earlier unlinks can leave a partial completed batch.
    """
    if type(execute) is not bool:
        raise ValueError('execute must be boolean')
    if type(max_files) is not int or not 1 <= max_files <= MAX_FILES:
        raise ValueError(f'max_files must be in 1..{MAX_FILES}')
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise ValueError(f'max_bytes must be in 1..{MAX_BYTES}')
    if os.name != 'posix':
        raise RuntimeError('immutable hires GC requires POSIX flock')
    import fcntl

    out = hb._owned(out)
    fd = os.open(out / '.build.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise hb.BundleError('build lock is not regular')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state': 'producer-busy', 'executed': False, 'removed': 0, 'bytes': 0}
        named = os.stat(out / '.build.lock', follow_symlinks=False)
        opened = os.fstat(fd)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise hb.BundleError('build lock replaced')
        plan = retention.plan_retention(out, now=now)
        selected = []
        logical_bytes = 0
        for item in plan['candidates']:
            path, identity = _candidate(out, item)
            size = identity['size']
            if len(selected) >= max_files:
                break
            if logical_bytes + size > max_bytes:
                continue
            selected.append((path, identity))
            logical_bytes += size
        # Even dry run reports only candidates whose identity still matches.
        for path, identity in selected:
            _same(path, identity)
        removed = 0
        removed_bytes = 0
        if execute:
            touched = set()
            try:
                for path, identity in selected:
                    _same(path, identity)
                    os.unlink(path)
                    removed += 1
                    removed_bytes += identity['size']
                    touched.add(path.parent)
            finally:
                primary_error = sys.exc_info()[0] is not None
                sync_error = None
                for folder in sorted(touched):
                    try:
                        hb._fsync_dir(folder)
                    except OSError as error:
                        if sync_error is None:
                            sync_error = error
                if sync_error is not None and not primary_error:
                    raise sync_error
        return {'state': 'executed' if execute else 'dry-run', 'executed': execute,
                'selected': len(selected), 'selectedBytes': logical_bytes,
                'removed': removed, 'bytes': removed_bytes,
                'candidateCount': plan['candidateCount'],
                'candidatesOmittedByPlanner': plan['candidatesOmitted'],
                # Planner telemetry: scan entries (cap retention.MAX_SCANNED), metadata bytes charged
                # (cap retention.MAX_METADATA_BYTES), protected root indexes, trusted backfill progress.
                'scanned': plan['scanned'], 'metadataBytes': plan['metadataBytes'],
                'retainedIndexes': plan['retainedIndexes'],
                'preparationProgressValid': plan['preparationProgressValid'],
                'candidates': [path.relative_to(out).as_posix() for path, _ in selected]}
    finally:
        os.close(fd)


def _bounded_int(name, high):
    def parse(text):
        try:
            value = int(text, 10)
        except ValueError:
            raise argparse.ArgumentTypeError(f'{name} must be an integer') from None
        if not 1 <= value <= high:
            raise argparse.ArgumentTypeError(f'{name} must be in 1..{high}')
        return value
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description='Bounded immutable hires GC: one dry-run (default) or '
                                                 'explicit --execute collection under the build lock.')
    parser.add_argument('--out', required=True, help='owned immutable hires store (absolute path)')
    parser.add_argument('--execute', action='store_true', help='unlink the selected candidates (default: dry run)')
    parser.add_argument('--max-files', type=_bounded_int('max-files', MAX_FILES), default=MAX_FILES)
    parser.add_argument('--max-bytes', type=_bounded_int('max-bytes', MAX_BYTES), default=MAX_BYTES)
    args = parser.parse_args(argv)
    try:
        result = collect(Path(args.out), execute=args.execute, max_files=args.max_files, max_bytes=args.max_bytes)
    except (OSError, ValueError, RuntimeError) as error:       # BundleError is a ValueError
        print(f'hires_gc: {type(error).__name__}: {error}'.replace('\n', ' ')[:500], file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())

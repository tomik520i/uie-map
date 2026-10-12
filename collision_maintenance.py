"""Bounded maintenance of an explicitly GC-enabled collision output; never enables GC."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

import collision_gc


def maintain(out):
    out = Path(out)
    # Validate before opening any status path. run_gc owns the same lock as export.
    collision_gc.check_owned_out(out)
    started = time.time()
    result = collision_gc.run_gc(out)
    report = {'format': 'UIE_COLLISION_MAINTENANCE1', 'startedAt': started,
              'finishedAt': time.time(), 'result': result}
    # An overlapping run must not replace the last useful report with 'busy'.
    if result['state'] != 'busy':
        path = out/'gc-status.json'
        tmp = path.with_name(f'.gc-status.{os.getpid()}.tmp')
        try:
            with tmp.open('w', encoding='utf-8') as stream:
                json.dump(report, stream, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = maintain(args.out)
    except (OSError, ValueError, collision_gc.GcRefused) as error:
        print(json.dumps({'state': 'error', 'reason': str(error)[:300]}))
        return 1
    print(json.dumps(report))
    return 0 if report['result']['state'] in ('done', 'busy') else 1


if __name__ == '__main__':
    sys.exit(main())

"""Initialize the world collision maintenance profile under the export lock.

Default dry-run; --apply writes the journal, profile, then enabled marker.
Never creates ownership, removes data, installs timers, or converts a legacy GC output.
Future exporter invocations must explicitly use --allow-world-gc.
"""
import argparse
import json
import math
import os
from pathlib import Path
import stat
import time

import collision_world_gc as gc


def initialize(out, *, apply=False, now=None):
    if type(apply) is not bool:raise ValueError('apply must be boolean')
    now=time.time() if now is None else now
    if type(now) not in (int,float) or not math.isfinite(now) or now<=0:
        raise ValueError('now must be a positive finite timestamp')
    out,store=gc.legacy.check_owned_out(Path(out))
    if os.name!='posix':raise ValueError('initialization requires POSIX flock')
    import fcntl
    fd=os.open(out/gc.legacy.LOCK_NAME,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):raise ValueError('export lock is not regular')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'state':'busy','applied':False}
        a=os.fstat(fd);b=(out/gc.legacy.LOCK_NAME).lstat()
        if (a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino):raise ValueError('export lock replaced')
        gc.legacy.check_owned_out(out)
        gc.store_api._check_store(store)
        if os.path.lexists(out/gc.legacy.STATE_NAME):raise ValueError('legacy GC ledger present; no conversion')
        profile=gc.legacy._regular(out/gc.PROFILE_MARKER)
        enabled=gc.legacy._regular(out/gc.legacy.ENABLED_MARKER)
        if enabled and not profile:raise ValueError('enabled legacy profile cannot be converted')
        has_journal=gc.legacy._regular(out/gc.legacy.JOURNAL_NAME)
        if (enabled or profile) and not has_journal:raise ValueError('existing profile is missing its journal')
        journal=gc.legacy.load_journal(out,store) if has_journal else {'updatedAt':now,'indexes':{}}
        current=gc.store_api._read_pointer(store)
        if current is not None:gc.store_api._read_index(store,current)  # exact hash, bounded canonical index
        if enabled:
            gc._profile(out)
            if current is not None and current not in journal['indexes']:
                raise ValueError('enabled journal does not protect current index')
            return {'state':'already-initialized','applied':False,'indexSha256':current}
        stamp=max(now,journal['updatedAt'])
        protected=dict(journal['indexes'])
        if current is not None:protected[current]=max(stamp,protected.get(current,stamp))
        prepared={'updatedAt':stamp,'indexes':protected}
        if apply:
            # All writes precede enabling. A crash may leave a journal/profile without enabled;
            # retry under this lock preserves and refreshes their protection, never resets it.
            gc.legacy.write_journal(out,store,prepared)
            if not profile:
                gc.cb._replace_atomic(out/gc.PROFILE_MARKER,b'UIE world collision retention profile\n')
                gc.cb._fsync_directory(out)
            gc.cb._replace_atomic(out/gc.legacy.ENABLED_MARKER,b'UIE world collision GC explicitly enabled\n')
            gc.cb._fsync_directory(out)
        return {'state':'initialized' if apply else 'dry-run','applied':apply,'indexSha256':current,
                'protectedIndexes':len(protected),'exporterRequires':'--allow-world-gc','filesDeleted':0}
    finally:os.close(fd)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args(argv)
    try:result=initialize(args.out,apply=args.apply)
    except (ValueError,OSError,gc.legacy.GcRefused) as error:
        print(json.dumps({'state':'refused','applied':False,'reason':str(error)[:500]}));return 1
    print(json.dumps(result));return 4 if result['state']=='busy' else 0

if __name__=='__main__':raise SystemExit(main())

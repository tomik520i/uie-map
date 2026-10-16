"""Repeat verified whole-world collision passes while keeping the proof cache warm.

No service installation, client, implicit GC activation or deletion. The export lock
is released BETWEEN passes so separately configured maintenance can run. Default is
one pass; --passes 0 explicitly requests a continuous operator-managed process.
Unchanged regions reuse reports, but their input fingerprints and objects are still
checked. Large worlds can take hours per pass: this is not a low-latency dirty queue.
"""
import argparse
import os
from pathlib import Path
import signal
import stat
import threading

import collision_world_runner as runner


def acquire_coordinator(out):
    import fcntl
    path=Path(out)/'.world-watch.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        opened=os.fstat(fd);named=os.lstat(path)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev,opened.st_ino)!=(named.st_dev,named.st_ino):
            raise ValueError('watch coordinator lock replaced or nonregular')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:os.close(fd);return None
        named=os.lstat(path)
        if (opened.st_dev,opened.st_ino)!=(named.st_dev,named.st_ino):
            raise ValueError('watch coordinator lock replaced')
        return fd
    except BaseException:
        os.close(fd);raise


def run(*,out,world,hires_store,tools,interval=900,passes=1,budget_bytes=30*1024**3,
        min_free_bytes=24*1024**3,allow_world_gc=False,stop=None,emit=None,
        pass_fn=runner.run_pass,lock_fn=runner.ce._lock,cache_factory=runner.publisher.PairProofCache):
    """Caller owns the coordinator lock. Busy exporters consume no pass and write no status."""
    if type(interval)is not int or not 30<=interval<=86400:raise ValueError('interval must be 30..86400 seconds')
    if type(passes)is not int or not 0<=passes<=100000:raise ValueError('passes must be 0..100000')
    if type(allow_world_gc)is not bool:raise ValueError('allow_world_gc must be bool')
    if type(budget_bytes)is not int or not 1024**3<=budget_bytes<=64*1024**3:raise ValueError('invalid store budget')
    if type(min_free_bytes)is not int or not 20*1024**3<=min_free_bytes<=1024*1024**3:raise ValueError('invalid disk reserve')
    stop=stop if stop is not None else threading.Event()
    emit=emit if emit is not None else lambda record:print(runner.json.dumps(record),flush=True)
    cache=cache_factory();completed=0;last_state='finished'
    while not stop.is_set() and (passes==0 or completed<passes):
        lock=lock_fn(Path(out))
        if lock is None:
            emit(dict(state='exporter-busy',passes=completed))
            stop.wait(min(interval,60));continue
        try:
            result=pass_fn(out=out,world=world,hires_store=hires_store,tools=tools,
                           cache=cache,budget_bytes=budget_bytes,min_free_bytes=min_free_bytes,
                           allow_world_gc=allow_world_gc)
            completed+=1
            last_state='partial' if result['state']=='partial' else 'finished'
            emit(dict(state='pass-finished',passes=completed,result=result))
            # A low disk condition requires operator attention, not an endless retry loop.
            if result['state']=='stopped-budget':return dict(state='stopped-budget',passes=completed)
            if result['state'] not in ('complete','partial'):
                raise ValueError('unexpected pass state: '+str(result['state']))
        finally:lock.close()
        if passes==0 or completed<passes:stop.wait(interval)
    return dict(state='stopped' if stop.is_set() else last_state,passes=completed)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    for name in ('world','catalog-dir','hires-store','out'):ap.add_argument('--'+name,required=True,type=Path)
    ap.add_argument('--interval',type=int,default=900)
    ap.add_argument('--passes',type=int,default=1)
    ap.add_argument('--budget-gib',type=int,default=30)
    ap.add_argument('--min-free-gib',type=int,default=24)
    ap.add_argument('--allow-world-gc',action='store_true')
    a=ap.parse_args()
    if (not 30<=a.interval<=86400 or not 0<=a.passes<=100000 or
        not 1<=a.budget_gib<=64 or not 20<=a.min_free_gib<=1024):ap.error('invalid limits')
    if os.name!='posix':ap.error('requires POSIX file locks')
    out=runner.ce.check_paths(a.out,world=a.world,hires=a.hires_store,catalog=a.catalog_dir)
    out.mkdir(parents=True,exist_ok=True);runner.region.stage._directory(out)
    coordinator=acquire_coordinator(out)
    if coordinator is None:
        print('{"state":"coordinator-busy"}');return 3
    try:
        def interrupted(signum,frame):raise KeyboardInterrupt('signal '+str(signum))
        for sig in (signal.SIGTERM,signal.SIGHUP):signal.signal(sig,interrupted)
        os.nice(15)
        result=run(out=out,world=a.world.resolve(),hires_store=a.hires_store.resolve(),
                   tools=runner.probe.Toolkit(a.catalog_dir.resolve()),interval=a.interval,passes=a.passes,
                   budget_bytes=a.budget_gib*1024**3,min_free_bytes=a.min_free_gib*1024**3,
                   allow_world_gc=a.allow_world_gc)
        print(runner.json.dumps(result));return 2 if result['state'] in ('stopped-budget','partial') else 0
    finally:os.close(coordinator)


if __name__=='__main__':raise SystemExit(main())

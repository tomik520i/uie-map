"""Resumable whole-surface collision export without a Minecraft client.

Private output until explicitly configured on the web server. One sequential pass
over all published surface regions, source-stable staging, bounded proof warm-up,
pointer-last publication. No deletion or automatic GC opt-in. A later pass catches changes;
reports are only reuse hints, rechecked against live sources and verified objects.
"""
from pathlib import Path
import argparse
import json
import os
import shutil
import signal
import time

import collision_bundle as cb
import collision_export as ce
import collision_region_probe as probe
import collision_world_region as region
import collision_world_store as publisher

FORMAT='UIE_COLLISION_WORLD_STATUS1'


def plan(visible):
    jobs={}
    for key in visible['tiles']:
        tx,tz=ce.parse_tile(key)
        jobs.setdefault((tx//8,tz//8),[]).append((tx,tz))
    # Dense/near areas first; the queue still contains the whole published world.
    return [(key,sorted(jobs[key])) for key in sorted(jobs,key=lambda p:(p[0]**2+p[1]**2,p))]


def write_json(path,doc):
    cb._replace_atomic(path,cb.canonical(doc));cb._fsync_directory(path.parent)


def load_report(path):
    try:
        raw=probe._read_bounded(path,2*1024**2,'region report')
        return probe._strict_json(raw,'region report')
    except (OSError,ValueError,probe.ProbeError):
        return None


def recover_scratch(out):
    """Only this export's temporary region copies; caller holds exclusive export lock."""
    root=Path(out)/'scratch'
    if not root.exists():return 0
    region.stage._directory(root)
    candidates=[]
    for path in root.iterdir():
        if (not path.name.startswith('collision-probe-') or path.is_symlink() or not path.is_dir()
                or not path.resolve().is_relative_to(root.resolve())):
            raise ValueError('unexpected scratch entry; kept: '+str(path))
        for child in path.iterdir():
            if child.is_symlink() or not child.is_file() or not child.name.startswith('r.') or not child.name.endswith('.mca'):
                raise ValueError('unexpected scratch contents; kept: '+str(child))
        candidates.append(path)
    for path in candidates:
        result=probe._cleanup(path)
        if result is not True:raise ValueError('scratch recovery failed: '+str(result))
    return len(candidates)


def cold_retry_candidate(report):
    """Only a stable, fully read region with hash mismatches can benefit from a fresh worker.

    This does not approve mismatches: the retry still has to reproduce every full published SHA.
    Source failures, unreadable chunks, stale tiles and failed cleanup are never retried here.
    """
    if not isinstance(report,dict) or report.get('ok') is not False or report.get('fatal') is not None:
        return False
    if report.get('scratchCleanup') is not True:return False
    source=report.get('source',{})
    if not source.get('before') or source.get('before')!=source.get('copy') or source.get('before')!=source.get('after'):
        return False
    if not source.get('registrySha256') or source.get('registrySha256')!=source.get('registryAfter'):return False
    tiles=report.get('tiles',{})
    results={v.get('result') for v in tiles.values() if isinstance(v,dict)}
    return bool(tiles) and all(isinstance(v,dict) for v in tiles.values()) and 'mismatch' in results and results <= {'match','unknown','mismatch'}


def check_gc_profile(out,allow_world_gc=False):
    """Read-only compatibility gate; caller holds the export lock. Never enables GC.

    Maintenance uses this same lock, so it cannot remove staged objects mid-pass.
    Publications already journal both old and new indexes before moving the pointer.
    """
    if type(allow_world_gc) is not bool:raise ValueError('allow_world_gc must be bool')
    out=Path(out)
    enabled=cb._gc_enabled(out/'store')
    if not allow_world_gc:
        if enabled:raise ValueError('world store GC requires explicit --allow-world-gc')
        return
    import collision_world_gc as gc
    try:
        owned,store=gc.legacy.check_owned_out(out)
        gc._profile(owned)
        gc.legacy._check_enabled(owned)
        gc.legacy.load_journal(owned,store)
    except (ValueError,OSError,gc.legacy.GcRefused) as error:
        raise ValueError('world GC profile refused: '+str(error)) from error


def run_pass(*,out,world,hires_store,tools,budget_bytes=30*1024**3,min_free_bytes=24*1024**3,
             max_regions=None,only_regions=None,cache=None,clock=time.time,stage_fn=region.stage_region,
             check_fn=region.accepted_updates,publish_fn=publisher.publish_region,
             disk_free=lambda p:shutil.disk_usage(p).free,allow_world_gc=False):
    """Caller holds out/.export.lock. Returns explicit complete/partial/budget stop.

    max_regions is for isolated integration tests only; normal CLI plans ALL regions.
    """
    out,world,hires_store=map(Path,(out,world,hires_store))
    for name in ('store','reports'):
        (out/name).mkdir(exist_ok=True);region.stage._directory(out/name)
    check_gc_profile(out,allow_world_gc)
    recovered=recover_scratch(out)
    planned=tools.read_index(hires_store)
    jobs=plan(planned);all_jobs=len(jobs)
    if only_regions is not None:
        wanted=set(only_regions)
        if not wanted or not wanted <= {key for key,_ in jobs}:raise ValueError('unknown selected region')
        jobs=[item for item in jobs if item[0] in wanted]
    if max_regions is not None:
        if type(max_regions)is not int or max_regions<1:raise ValueError('max_regions must be positive')
        jobs=jobs[:max_regions]
    cache=publisher.PairProofCache() if cache is None else cache
    expected=publisher._read_pointer(out/'store')
    status=dict(format=FORMAT,state='running',startedAt=clock(),updatedAt=clock(),totalRegions=all_jobs,
                selectedRegions=len(jobs),completedRegions=0,reusedRegions=0,failedRegions=0,coldRetries=0,
                publishedTiles=len(publisher._read_index(out/'store',expected)) if expected else 0,
                indexSha256=expected,activeRegion=None,errors=[],scope='published-surface-world',
                plannedTiles=len(planned['tiles']),planIndexSha256=planned.get('indexSha256'))
    status['recoveredScratchDirectories']=recovered
    def save():
        status['updatedAt']=clock();write_json(out/'status.json',status)
    save()
    try:
        for (jx,jz),tiles in jobs:
            status.update(activeRegion=[jx,jz],phase='checking');save()
            reserve=640*1024**2
            if region.logical_bytes(out)+reserve>budget_bytes or disk_free(out)-reserve<min_free_bytes:
                status.update(state='stopped-budget',reason='store budget or disk reserve');break
            path=out/'reports'/f'{jx},{jz}.json'
            report=load_report(path);reused=False
            def render_report():
                status['phase']='rendering';save()
                rendered=stage_fn(jx,jz,out=out,world=world,hires_store=hires_store,tools=tools,tiles=tiles,
                                  budget_bytes=budget_bytes,min_free_bytes=min_free_bytes,disk_free=disk_free)
                write_json(path,rendered)
                if cold_retry_candidate(rendered) and callable(getattr(tools,'fresh',None)):
                    status['coldRetries']+=1;save()
                    previous=dict(counts=rendered.get('counts'),seconds=rendered.get('seconds'),
                                  geometry={k:v.get('geometrySha256') for k,v in rendered['tiles'].items() if v.get('result')=='mismatch'})
                    # stage_region independently rechecks disk reservation and takes a fresh stable copy.
                    rendered=stage_fn(jx,jz,out=out,world=world,hires_store=hires_store,tools=tools.fresh(),tiles=tiles,
                                      budget_bytes=budget_bytes,min_free_bytes=min_free_bytes,disk_free=disk_free)
                    rendered['coldRetry']=dict(reason='stable-geometry-mismatch',previousAttempt=previous)
                    write_json(path,rendered)
                return rendered
            try:
                if isinstance(report,dict) and report.get('region')==[jx,jz] and set(report.get('tiles',{}))=={f'{x},{z}' for x,z in tiles}:
                    try:
                        updates=check_fn(report,world=world,hires_store=hires_store,tools=tools);reused=True
                    except (ValueError,KeyError,TypeError,OSError,ce.ExportError,ce.TileError,probe.ProbeError):
                        report=None
                else:report=None
                if report is None:
                    report=render_report()
                # A report can be staged but failed; no update is accepted until all gates pass.
                stalled=0;warmup_started=clock()
                while True:
                    updates=check_fn(report,world=world,hires_store=hires_store,tools=tools)
                    if region.logical_bytes(out)+64*1024**2>budget_bytes or disk_free(out)-64*1024**2<min_free_bytes:
                        status.update(state='stopped-budget',reason='publication metadata reserve');break
                    status['phase']='verifying';save()
                    # Recheck opt-in on every attempt, including long warm-up loops.
                    # Operator changes must use this lock; a changed profile must not silently publish.
                    check_gc_profile(out,allow_world_gc)
                    before_proofs=len(cache);verification_budget=publisher.Budget(1024,256*1024**2)
                    try:
                        result=publish_fn(out/'store',expected_index=expected,updates=updates,removals=set(),
                                          cache=cache,budget=verification_budget)
                        break
                    except publisher.WarmupPending:
                        status['phase']='warming-cache';save()
                        stalled=stalled+1 if len(cache)<=before_proofs and verification_budget.verified==0 else 0
                        if stalled>=3 or clock()-warmup_started>3600:
                            raise ValueError('warmup-stalled: no progress or one-hour deadline')
                    except publisher.StoreError:
                        if not reused or publisher._read_pointer(out/'store')!=expected:raise
                        reused=False;report=render_report()  # one repair attempt for missing staged objects
                if status['state']=='stopped-budget':break
                expected=result['indexSha256'];status['indexSha256']=expected
                status['publishedTiles']=result['entries'];status['completedRegions']+=1
                status['reusedRegions']+=int(reused);status['phase']='published';save()
            except (ValueError,KeyError,TypeError,OSError,ce.ExportError,ce.TileError,probe.ProbeError) as error:
                # Failures are visible and the next pass can retry; never drop other successful regions.
                status['failedRegions']+=1
                status['errors']=(status['errors']+[dict(region=[jx,jz],message=str(error)[:500])])[-32:]
                status['phase']='region-error';save()
        else:
            status['state']='complete' if not status['failedRegions'] and len(jobs)==all_jobs else 'partial'
        status['finishedAt']=clock();status['activeRegion']=None;save();return status
    except BaseException as error:
        status.update(state='interrupted',reason=type(error).__name__+': '+str(error)[:400]);save();raise


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    for name in ('world','catalog-dir','hires-store','out'):ap.add_argument('--'+name,required=True,type=Path)
    ap.add_argument('--budget-gib',type=int,default=30)
    ap.add_argument('--min-free-gib',type=int,default=24)
    ap.add_argument('--allow-world-gc',action='store_true',help='accept an already initialized world GC profile; never enables or runs deletion')
    a=ap.parse_args()
    if not 1<=a.budget_gib<=64 or not 20<=a.min_free_gib<=1024:ap.error('invalid disk limits')
    out=ce.check_paths(a.out,world=a.world,hires=a.hires_store,catalog=a.catalog_dir)
    out.mkdir(parents=True,exist_ok=True);region.stage._directory(out)
    if os.name!='posix':raise RuntimeError('world export requires POSIX locking and proof cache')
    lock=ce._lock(out)
    if lock is None:raise RuntimeError('another exporter holds the lock')
    try:
        def stopped(signum,frame):raise KeyboardInterrupt('signal '+str(signum))
        for sig in (signal.SIGTERM,signal.SIGHUP):signal.signal(sig,stopped)
        os.nice(15)
        result=run_pass(out=out,world=a.world.resolve(),hires_store=a.hires_store.resolve(),
                        tools=probe.Toolkit(a.catalog_dir.resolve()),budget_bytes=a.budget_gib*1024**3,
                        min_free_bytes=a.min_free_gib*1024**3,allow_world_gc=a.allow_world_gc)
        print(json.dumps(result));return 0 if result['state']=='complete' else 2
    finally:lock.close()


if __name__=='__main__':raise SystemExit(main())

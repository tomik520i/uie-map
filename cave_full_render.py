"""Explicit full-world cave render supervisor, resuming the existing owned store.

One worker, one region at a time; continues without manual batch restarts.
Published coverage is not a simultaneous snapshot of a running Minecraft world.
No client, game restart, deletion, timer installation or public web deployment.

A batch that rendered its region but stopped in the publish phase (deadline / incomplete / verify limit) is
finished by bounded publication-only ticks with the same PublishSession; "nothing pending" with incomplete
coverage gets one bounded publication attempt and must then show progress. Maintenance errors are counted per
collector kind; three consecutive errors stop the run (producer-busy is neither success nor error).
A valid geometry pointer whose cap pointer still names the previous geometry (PublicationPending, e.g. after a
stop between the two commits) gets one bounded publication-only recovery before coverage is evaluated again;
any other coverage error stays fatal.
"""
import argparse,fcntl,hashlib,json,os,re,signal,time
from pathlib import Path
import cave_batch as batch
import hires_bundle as hb
from hires_publish_session import PublishSession
from world_hires import plan_jobs
import cave_gc,hires_gc

STATUS='cave-full-status.json'
# Publication-only recovery after a batch rendered its region but stopped in its publish phase.
RECOVERABLE_PUBLISH_REASONS=('publisher-deadline','publisher-incomplete','publisher-verify-limit')
TRANSIENT_PUBLISH_STATES=('verifying','preparing','producer-busy','waiting-for-producer')
RECOVERY_SECONDS=1800          # additional monotonic seconds per recovery, never beyond the run deadline
RECOVERY_TICKS=600             # > 62,912 tiles / 128 per tick (cold verification of a whole world)
RECOVERY_WAIT_TICKS=30         # busy/waiting ticks per recovery (2 s apart)
RECOVERY_WAIT_SECONDS=2
MAINTENANCE_FAILURE_LIMIT=3
METADATA_WARNING_BYTES=200*1024**2


class RecoveryError(RuntimeError):
    pass


class PublicationPending(ValueError):
    """Both pointers are valid and hash-verified, but the cap index belongs to another geometry generation.

    This is the state between the geometry pointer commit and the cap commit of one publication (e.g. the process
    stopped in between). It is the ONLY coverage error run() treats as recoverable; every other error (missing or
    corrupt pointer/index, schema, job data) stays a hard failure."""


def changing_snapshot(result):
    """Only a fresh, reaped renderer reporting the known live-save race is retryable."""
    renderer=result.get('lastRenderer') or {}
    status=renderer.get('status') or {}
    return (result.get('reason')=='renderer-exit-1' and renderer.get('returncode')==1
            and renderer.get('fresh') is True and renderer.get('groupGone') is True
            and isinstance(status.get('state'),str)
            and re.fullmatch(r'stopped: build failed: OverviewError: r\.-?\d+\.-?\d+\.mca kept changing while it was copied',status['state']) is not None)


def _publish_once(out,session):
    from hires_publisher import run_once
    return run_once(out,session,world='uie',dimension='0',cave_pairs=True)


def recover_publication(out,session,*,heartbeat=lambda ticks,state:None,publish=None,max_seconds=RECOVERY_SECONDS,
                        max_ticks=RECOVERY_TICKS,max_wait_ticks=RECOVERY_WAIT_TICKS,run_deadline=None,
                        monotonic=time.monotonic,sleep=time.sleep):
    """Publication-only ticks with the SAME PublishSession (its verification proofs survive); never renders.

    Succeeds only when geometry AND caps are published. Only known transient states are retried, bounded by
    ticks, by busy/waiting ticks and by monotonic time (also by the supervisor's own deadline). heartbeat(ticks,
    state) runs after every tick. Anything else raises RecoveryError."""
    publish=publish or (lambda current:_publish_once(out,current))
    limit=monotonic()+max_seconds
    if run_deadline is not None:limit=min(limit,run_deadline)
    ticks=waits=0
    while True:
        if ticks>=max_ticks:raise RecoveryError(f'publication recovery tick limit {max_ticks} reached')
        if monotonic()>=limit:raise RecoveryError('publication recovery time limit reached')
        ticks+=1
        try:record=publish(session)
        except (OSError,ValueError) as error:
            raise RecoveryError(f'publication recovery failed: {type(error).__name__}: {error}'[:300]) from error
        state=record.get('state') if isinstance(record,dict) else None
        heartbeat(ticks,state)
        if isinstance(record,dict) and record.get('published') is True:
            cap=record.get('capPublication')
            if isinstance(cap,dict) and cap.get('published') is True:
                return {'ticks':ticks,'indexSha256':record.get('indexSha256'),'capIndexSha256':cap.get('indexSha256')}
            raise RecoveryError('publication recovery: geometry published but caps not published '
                                f"({cap.get('state') if isinstance(cap,dict) else 'missing'})")
        if state not in TRANSIENT_PUBLISH_STATES:
            raise RecoveryError(f'publication recovery stopped: {state}')
        if state in ('producer-busy','waiting-for-producer'):
            waits+=1
            if waits>=max_wait_ticks:raise RecoveryError(f'publication recovery still {state} after {waits} ticks')
            sleep(RECOVERY_WAIT_SECONDS)


def track_maintenance(results,failures):
    """Per-kind outcome of one maintain() call -> (summary, failures, warning); run() fails at 3 consecutive errors.

    error: a collector raised; success: a scan/execution finished ('executed'/'dry-run'); producer-busy (or no
    report) changes nothing. Only an actual success resets the kind's counter."""
    failures=dict(failures);summary={};warning=None
    for kind in ('geometry','caps'):
        reports=[r for r in results if r.get('kind')==kind]
        if any(r.get('state')=='error' for r in reports):outcome='error'
        elif any(r.get('state') in ('executed','dry-run') for r in reports):outcome='ok'
        elif reports:outcome='busy'
        else:outcome='none'
        metadata=max([r['metadataBytes'] for r in reports if type(r.get('metadataBytes')) is int],default=None)
        summary[kind]={'state':outcome,'removed':sum(r.get('removed',0) for r in reports if type(r.get('removed')) is int),
                       'metadataBytes':metadata}
        if outcome=='error':
            failures[kind]=failures.get(kind,0)+1
            summary[kind]['error']=next(str(r.get('error'))[:300] for r in reports if r.get('state')=='error')
        elif outcome=='ok':failures[kind]=0
        if metadata is not None and metadata>METADATA_WARNING_BYTES:
            warning=f'{kind} retention metadata {metadata} bytes exceeds {METADATA_WARNING_BYTES}'
    return summary,failures,warning

def maintain(out):
    """Bounded old map-data cleanup; each collector takes its own build lock.

    Run root GC first: alias unlink subsequently refreshes ctime and naturally
    postpones root-object reclamation until a later run's grace expires.
    """
    results=[]
    for kind,collector,passes in [('geometry',hires_gc.collect,2),('caps',cave_gc.collect,4)]:
        for _ in range(passes):
            try:
                report=collector(out,execute=True)
                results.append({'kind':kind,**report})
                if not report.get('removed'):break
            except (OSError,ValueError) as error:
                results.append({'kind':kind,'state':'error','error':str(error)[:300]});break
    return results

def coverage(out,world):
    """Count only error-free jobs whose exact cap pairs are in the published index."""
    planned=plan_jobs(Path(world)/'region')
    raw,_=hb._read(out/'cave-data/current.json',4096)
    pointer=hb._strict_json(raw,'cap pointer');sha=pointer['indexSha256']
    if not isinstance(sha,str) or not hb.HEX64.fullmatch(sha):raise ValueError('invalid cap pointer')
    raw,_=hb._read(out/'cave-data/indexes'/f'{sha}.json',hb.MAX_INDEX_BYTES)
    if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('cap index hash')
    index=hb._strict_json(raw,'cap index')
    if (not isinstance(index,dict) or index.get('format')!='UIE_CAVE_CAP_INDEX1' or not isinstance(index.get('pairs'),dict)
            or not isinstance(index.get('geometryIndexSha256'),str) or not hb.HEX64.fullmatch(index['geometryIndexSha256'])):
        raise ValueError('invalid cap index')
    current=hb._current_index(out)                # validated geometry pointer + index, or None, or raises
    if current is None:raise ValueError('no published geometry index')
    if index['geometryIndexSha256']!=current[0]:
        raise PublicationPending(f"cap index {sha[:12]} belongs to geometry {index['geometryIndexSha256'][:12]}, "
                                 f'current geometry is {current[0][:12]}')
    covered=failed=0
    for x,z in planned:
        try:raw,_=hb._read(out/'jobs'/f'{x}_{z}.json',16*1024**2)
        except FileNotFoundError:continue
        job=hb._strict_json(raw,'job')
        if job.get('job')!=[x,z]:raise ValueError('job coordinates differ')
        if job.get('errors')!=[]:failed+=1;continue
        pairs=job.get('capPairs');tiles=job.get('tiles')
        if not isinstance(pairs,dict) or not isinstance(tiles,dict) or set(pairs)!=set(tiles):continue
        if all(index['pairs'].get(k)==v for k,v in pairs.items()):covered+=1
    return {'totalRegions':len(planned),'publishedRegions':covered,'failedRegions':failed,
            'remainingRegions':len(planned)-covered,'publishedTiles':len(index['pairs']),
            'geometryIndexSha256':current[0],'capIndexSha256':sha}

def run(out,world,catalog,*,wait_unit=None,max_seconds=7*86400,sleep=time.sleep):
    import subprocess
    out=batch.check_store(Path(out));world=Path(world);catalog=Path(catalog)
    fd=os.open(out/'.cave-full-lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        started=time.time();deadline=time.monotonic()+max_seconds
        doc={'format':'UIE_CAVE_FULL_STATUS1','state':'running','startedAt':started,
             'updatedAt':started,'completedSteps':0,'scope':'entire-saved-world',
             'storeBudgetGiB':80,'expansionReserveGiB':24}
        def save(state,**fields):
            doc.update(fields,state=state,updatedAt=time.time());hb._atomic(out/STATUS,json.dumps(doc,sort_keys=True).encode())
        try:
            if wait_unit:
                while True:
                    p=subprocess.run(['systemctl','--user','show',wait_unit,'--property=ActiveState','--value'],capture_output=True,text=True,check=True)
                    if p.stdout.strip() not in ('active','activating','deactivating'):break
                    save('waiting-for-current-render',waitingUnit=wait_unit)
                    if time.monotonic()>=deadline:raise TimeoutError('full render deadline')
                    sleep(15)
            session=PublishSession();consecutive_failures=0
            failures={'geometry':0,'caps':0}
            command=batch.renderer_command(out,world,catalog,(768,256))
            command+=['--cave-store-budget-mib',str(80*1024),'--cave-reserve-gib','24']

            def recover():
                # Same session as the batches; heartbeat on every tick keeps the status fresh.
                def heartbeat(ticks,state):
                    save('running',publicationRecovery={'ticks':ticks,'state':str(state)[:40]})
                outcome=recover_publication(out,session,heartbeat=heartbeat,run_deadline=deadline,sleep=sleep)
                save('running',publicationRecovery={'ticks':outcome['ticks'],'state':'published'})

            def step_completed():
                nonlocal consecutive_failures,failures
                doc['completedSteps']+=1;consecutive_failures=0
                summary,failures,warning=track_maintenance(maintain(out),failures)
                save('running',maintenance=summary,maintenanceFailures=failures,maintenanceWarning=warning)
                exhausted=[kind for kind,count in failures.items() if count>=MAINTENANCE_FAILURE_LIMIT]
                if exhausted:
                    raise RuntimeError(f'maintenance failed {MAINTENANCE_FAILURE_LIMIT} consecutive times: '+','.join(exhausted))

            pending_recovered=False
            while time.monotonic()<deadline:
                try:progress=coverage(out,world)
                except PublicationPending as pending:
                    # Geometry committed but caps not (e.g. stop between both commits): finish publication only,
                    # with the same session, then evaluate coverage again. A second mismatch right after is fatal.
                    if pending_recovered:
                        raise RuntimeError(f'geometry and cap publication still differ after recovery: {pending}') from pending
                    save('running',publicationPending=str(pending)[:200])
                    recover();pending_recovered=True;continue
                pending_recovered=False
                save('running',**progress)
                if progress['totalRegions']>0 and progress['remainingRegions']==0:
                    save('completed',finishedAt=time.time());return doc
                result=batch.run_batch(out,world=world,catalog_dir=catalog,world_id='uie',dimension_id='0',
                    max_steps=1,session=session,max_verify_ticks=512,publish_deadline=900,
                    render=lambda step:batch.render_subprocess(out,step,command,timeout=900))
                state=result['state'];reason=result.get('reason')
                if state=='completed':
                    step_completed();continue
                if state=='error' and reason in RECOVERABLE_PUBLISH_REASONS:
                    # The region is rendered; finish ITS publication only (no re-render, same session).
                    recover();step_completed();continue
                if state=='idle' and reason=='nothing-pending':
                    # Nothing left to render, yet coverage is incomplete: publish once, then demand progress.
                    recover()
                    after=coverage(out,world)
                    if after['remainingRegions']>0 and after.get('publishedRegions',0)<=progress.get('publishedRegions',0):
                        raise RuntimeError('full render made no progress: nothing pending but coverage incomplete')
                    continue
                if state=='busy':save('waiting-for-current-render');sleep(30);continue
                if state=='idle' and reason=='backoff':save('waiting-for-retry');sleep(60);continue
                if state=='error' and (reason in ('render-failed','renderer-timeout') or changing_snapshot(result)):
                    consecutive_failures+=1
                    if consecutive_failures<10:
                        save('retrying-other-regions',lastReason=reason);sleep(10);continue
                raise RuntimeError(f'full render stopped: {state}/{reason}')
            raise TimeoutError('full render exceeded seven-day execution budget')
        except BaseException as error:
            save('error',lastReason=f'{type(error).__name__}: {error}'[:500]);raise
    finally:os.close(fd)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out',type=Path,required=True);ap.add_argument('--world',type=Path,required=True)
    ap.add_argument('--catalog-dir',type=Path,required=True);ap.add_argument('--wait-unit')
    a=ap.parse_args();signal.signal(signal.SIGTERM,batch._terminate)
    run(a.out,a.world,a.catalog_dir,wait_unit=a.wait_unit)

if __name__=='__main__':main()

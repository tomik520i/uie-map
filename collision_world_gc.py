"""Bounded whole-world collision GC. Explicit opt-in; not enabled by first-pass runner.

Three separate runs/phases: old indexes, unreferenced pairs, unreferenced objects.
Each phase has its own first-observed grace. The caller keeps a proof cache across
ticks; a cold cache yields warming without ledger writes or deletion. Default dry run.

Required in `out`: the legacy ownership marker, the world profile marker, the legacy enabled marker and a valid
publication journal. A legacy GC ledger (gc-state.json) in the same output is refused: the two GCs must never share
an output (the legacy executor itself cannot be stopped from here; see the module review notes).

Graph safety while deleting (caller holds out/.export.lock):
  - the ledger with every observation is written BEFORE the first unlink;
  - one full scan right after it must equal the plan's listing (known names AND unknown count/examples); the identity
    (dev, ino, mtime, ctime) of indexes/, pairs/ and objects/ is taken before and after that scan;
  - before every unlink: pointer hash, path pattern, parent type and file identity (legacy check) plus ctime, and the
    folder identities: no folder may be replaced, and every folder other than the phase folder must be byte-for-byte
    the same directory state (a new index stops pair deletion, a new pair stops object deletion). The phase folder
    itself only changes by our own unlinks; additions there cannot create references into it;
  - unknown names (temporaries, foreign files) are never candidates and never block an unchanged store;
  - the phase directory is fsynced whenever anything was deleted, also after a failure in the middle.
"""
from pathlib import Path
import os
import stat
import time

import collision_bundle as cb
import collision_gc as legacy
import collision_world_store as store_api
import collision_world_retention as retention

STATE_NAME='world-gc-state.json'
STATE_FORMAT='UIE_COLLISION_WORLD_GC_STATE1'
RESULT_FORMAT='UIE_COLLISION_WORLD_GC_RESULT1'
PROFILE_MARKER='.uie-collision-world-gc'
FOLDERS=retention.PHASES


class RetentionProofCache(store_api.PairProofCache):
    """Same full verifier as publisher; independent capacity for retained historical revisions."""
    def __init__(self,max_entries=300000):
        if type(max_entries)is not int or not 1<=max_entries<=500000:
            raise ValueError('retention proof capacity must be 1..500000')
        self.max_entries=max_entries;self._proofs={}


def load_ledger(out,store,limits):
    path=out/STATE_NAME
    if not os.path.lexists(path):return {}
    doc=store_api._strict_json(store_api._read(path,limits.max_ledger_bytes+4096),STATE_NAME)
    if (not isinstance(doc,dict) or set(doc)!={'format','store','updatedAt','pointerSha256','entries'}
            or doc['format']!=STATE_FORMAT or doc['store']!=str(store)
            or not retention._finite(doc['updatedAt']) or not isinstance(doc['entries'],dict)):
        raise ValueError('invalid world GC ledger')
    store_api._check_sha(doc['pointerSha256'],'ledger pointer hash')
    if len(doc['entries'])>limits.max_ledger_entries:raise ValueError('ledger entry limit')
    for rel,item in doc['entries'].items():retention._validate_entry(rel,item)
    if retention._ledger_bytes(doc['entries'])>limits.max_ledger_bytes:raise ValueError('ledger byte limit')
    return doc['entries']


def write_ledger(out,store,entries,now,pointer,limits):
    if len(entries)>limits.max_ledger_entries or retention._ledger_bytes(entries)>limits.max_ledger_bytes:
        raise ValueError('world GC ledger exceeds limits')
    for rel,item in entries.items():retention._validate_entry(rel,item)
    payload=cb.canonical(dict(format=STATE_FORMAT,store=str(store),updatedAt=now,pointerSha256=pointer,entries=entries))
    cb._replace_atomic(out/STATE_NAME,payload);cb._fsync_directory(out)


def _profile(out):
    if not stat.S_ISREG((out/PROFILE_MARKER).lstat().st_mode):
        raise ValueError('world GC profile marker missing or not regular')
    if os.path.lexists(out/legacy.STATE_NAME):
        raise ValueError('legacy GC ledger present in this output: world and legacy GC must not share an output')


def _listing(store,limits):
    files,unknown=retention._scan(store,limits.max_files)
    return {folder:set(items) for folder,items in files.items()},unknown


def _dir_identity(path):
    info=os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f'{path.name} is not a real directory')
    return (info.st_dev,info.st_ino,info.st_mtime_ns,info.st_ctime_ns)


def _snapshot(store,limits,plan):
    """Folder identities that belong to a listing equal to the plan's (taken before and after the scan)."""
    before={folder:_dir_identity(store/folder) for folder in FOLDERS}
    listing,unknown=_listing(store,limits)
    after={folder:_dir_identity(store/folder) for folder in FOLDERS}
    if before!=after:
        raise ValueError('store folders changed while they were listed')
    known={k:set(v) for k,v in plan['known'].items()}
    if listing!=known or len(unknown)!=plan['unknown']['count'] or unknown[:50]!=plan['unknown']['examples']:
        raise ValueError('store listing changed since planning')
    return after


def _check_folders(store,snapshot,phase):
    for folder,expected in snapshot.items():
        current=_dir_identity(store/folder)
        if current[:2]!=expected[:2]:
            raise ValueError(f'{folder} directory was replaced')
        if folder!=phase and current!=expected:
            raise ValueError(f'{folder} directory changed since planning (another writer)')


def gc_world_locked(out,*,cache,budget,now=None,limits=None,dry_run=True,unlink=os.unlink):
    """Caller holds out/.export.lock exclusively. No recursive deletion; only known immutable files."""
    now=time.time() if now is None else now
    limits=retention.WorldRetentionLimits() if limits is None else limits
    result=dict(format=RESULT_FORMAT,state='starting',at=now,dryRun=dry_run,deleted=[],deletedBytes=0)
    try:
        if type(dry_run) is not bool:
            raise ValueError('dry_run must be a bool')
        out,store=legacy.check_owned_out(out);_profile(out);legacy._check_enabled(out)
        journal=legacy.load_journal(out,store)
        entries=load_ledger(out,store,limits)
        plan=retention.plan_world_retention(store,now=now,journal=journal,cache=cache,budget=budget,limits=limits)
        decision=retention.select_deletions(plan,entries,now=now,limits=limits)
        result.update(phase=decision['phase'],candidates={k:len(v) for k,v in plan['candidates'].items()},
                      eligible=len(decision['selected']),selectedBytes=decision['selectedBytes'])
        if dry_run:
            result['state']='dry-run';return result
        # Persist observations before any unlink. Crash can only extend protection.
        entries=decision['entries'];write_ledger(out,store,entries,now,plan['pointerSha256'],limits)
        snapshot=_snapshot(store,limits,plan)
        phase=decision['phase']
        # Persist any earlier phase's removals before deleting its descendants,
        # including a prior tick that failed its final directory fsync.
        if decision['selected']:
            for parent in {'indexes':(), 'pairs':('indexes',), 'objects':('indexes','pairs')}[phase]:
                cb._fsync_directory(store/parent)
        try:
            for item in decision['selected']:
                # Legacy checks strict allowlist, parent type, pointer hash and inode/size/mtime.
                legacy._check_before_unlink(store,item['path'],item,plan['pointerSha256'])
                target=store/item['path']
                if retention._identity(target.lstat())!={k:item[k] for k in retention.IDENTITY_FIELDS}:
                    raise ValueError('candidate ctime or identity changed')
                _check_folders(store,snapshot,phase)
                unlink(target)
                result['deleted'].append(item['path']);result['deletedBytes']+=item['size']
                entries.pop(item['path'],None)
        finally:
            if result['deleted']:
                try:
                    cb._fsync_directory(store/phase);result['phaseFsynced']=True
                except OSError as error:
                    result['phaseFsynced']=False;result['fsyncError']=f'{type(error).__name__}: {error}'[:300]
        if result.get('fsyncError'):
            raise OSError('phase directory fsync failed: '+result['fsyncError'])
        write_ledger(out,store,entries,now,plan['pointerSha256'],limits)
        # Only forget expired publications when their immutable index no longer exists.
        retained={sha:at for sha,at in journal['indexes'].items()
                  if now-at<retention.MIN_SECONDS or (store/'indexes'/(sha+'.json')).exists()}
        if retained!=journal['indexes']:
            legacy.write_journal(out,store,dict(updatedAt=max(now,journal['updatedAt']),indexes=retained))
        result['state']='done'
    except retention.WarmupPending as error:
        result.update(state='warming',reason=str(error)[:500])
    except Exception as error:
        result.update(state='stopped' if result['deleted'] else 'refused',reason=type(error).__name__+': '+str(error)[:500])
        # A failed final ledger write leaves the earlier safe ledger; missing files vanish on the next plan.
    return result


def run_gc(out,**kwargs):
    try:
        out_path,_store=legacy.check_owned_out(Path(out))
        lock=legacy.default_export_lock(out_path)
    except (legacy.GcRefused,OSError) as error:
        return dict(format=RESULT_FORMAT,state='refused',reason=str(error)[:500],deleted=[])
    if lock is None:return dict(format=RESULT_FORMAT,state='busy',deleted=[])
    try:return gc_world_locked(out_path,**kwargs)
    finally:lock.close()

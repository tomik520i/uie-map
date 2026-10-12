"""Read-only preflight for a bounded cave pilot, under the caller's build lock.

Counts unique inodes inside the owned store; links outside it are not deducted.
This is conservative store accounting, not reclaimable bytes. No reservation can
guarantee free disk against unrelated writers; renderer must retain its floor.
"""
import os,stat,shutil
import hires_bundle as hb
import cave_retention

# Never admit more directory entries than the subsequent retention scan can read.
# This scan includes the whole store, so it is at least as conservative.
MAX_ENTRIES=cave_retention.MAX_SCANNED
ENTRY_RESERVE=4096  # headroom for the one-region producer and its publication
MIN_FREE_BYTES=20*1024**3
DEFAULT_RESERVE_BYTES=512*1024**2

def inspect_budget(out,*,max_store_bytes,reserve_bytes=DEFAULT_RESERVE_BYTES):
    for name,value in [('max_store_bytes',max_store_bytes),('reserve_bytes',reserve_bytes)]:
        if type(value) is not int or value<=0:raise ValueError(f'{name} must be a positive integer')
    out=hb._owned(out);stack=[out];inodes=set();scanned=logical=allocated=0
    allocation_known=True
    while stack:
        folder=stack.pop();hb._real_dir(folder)
        with os.scandir(folder) as entries:
            for entry in entries:
                scanned+=1
                if scanned>MAX_ENTRIES:raise hb.BundleError('cave budget scan capacity exceeded')
                path=folder/entry.name
                info=os.stat(path,follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):raise hb.BundleError('symlink in cave budget store')
                if stat.S_ISDIR(info.st_mode):stack.append(path);continue
                if not stat.S_ISREG(info.st_mode):raise hb.BundleError('nonregular file in cave budget store')
                key=(info.st_dev,info.st_ino)
                if key in inodes:continue
                inodes.add(key);logical+=info.st_size
                blocks=getattr(info,'st_blocks',None)
                if blocks is None:allocation_known=False
                else:allocated+=blocks*512
    # Count at least logical bytes, also accounting filesystem blocks if known.
    accounted=max(logical,allocated) if allocation_known else logical
    free=shutil.disk_usage(out).free
    reasons=[]
    if scanned+ENTRY_RESERVE>MAX_ENTRIES:reasons.append('entry-budget')
    if accounted+reserve_bytes>max_store_bytes:reasons.append('store-budget')
    if free-reserve_bytes<MIN_FREE_BYTES:reasons.append('free-disk-floor')
    return {'state':'blocked' if reasons else 'ready','reasons':reasons,'scanned':scanned,
            'maxEntries':MAX_ENTRIES,'reserveEntries':ENTRY_RESERVE,
            'uniqueFiles':len(inodes),'logicalUniqueBytes':logical,
            'allocatedUniqueBytes':allocated if allocation_known else None,
            'accountedBytes':accounted,'maxStoreBytes':max_store_bytes,
            'reserveBytes':reserve_bytes,'freeBytes':free,'minFreeBytes':MIN_FREE_BYTES}

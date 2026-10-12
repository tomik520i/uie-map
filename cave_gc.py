"""Bounded cave-only collection. Dry run by default; no scheduler or service.

Fresh protection scan under the producer lock, never caller-supplied candidates.
Only cave-data content-addressed names can be unlinked. Root store, jobs, client
artifacts and game world are outside the allowlist. Byte counts are logical,
never a promise of physical savings. A failed batch can have partial removals.
Removing hardlinks changes shared inode ctime; root GC may defer those objects
for another grace period. An oversized first candidate blocks this bounded batch.
"""
import argparse,json,os,stat,sys
from pathlib import Path
import hires_bundle as hb
import hires_retention as hr
import cave_retention as retention

MAX_FILES=64
MAX_BYTES=16*1024*1024
LIMITS={'indexes':retention.MAX_CAP_INDEX_BYTES,'pairs':retention.MAX_PAIR_BYTES,
        'caps':retention.MAX_CAP_BYTES,'objects':hb.MAX_OBJECT_BYTES}

class PartialCollectionError(OSError):
    def __init__(self,error,paths,logical_bytes):
        super().__init__(f'{error}; after removing {len(paths)} cave file(s)')
        self.report={'state':'partial-error','error':str(error),'removed':len(paths),
                     'removedPaths':paths,'removedLogicalBytes':logical_bytes,'reclaimableBytes':None}

def _candidate(out,item):
    path=item.get('path') if isinstance(item,dict) else None
    parts=path.split('/') if isinstance(path,str) else []
    if len(parts)!=3 or parts[0]!='cave-data' or parts[1] not in LIMITS:
        raise hb.BundleError('candidate outside cave directories')
    kind,name=parts[1:];suffix=retention.KINDS[kind]
    if not name.endswith(suffix) or not hb.HEX64.fullmatch(name[:-len(suffix)]):
        raise hb.BundleError('invalid cave candidate name')
    identity=item.get('identity')
    if (not isinstance(identity,dict) or set(identity)!={'device','inode','size','mtimeNs','ctimeNs'}
            or any(type(v) is not int or v<0 for v in identity.values())
            or not 1<=identity['size']<=LIMITS[kind]):
        raise hb.BundleError('invalid cave candidate identity')
    hb._real_dir(out/'cave-data');hb._real_dir(out/'cave-data'/kind)
    return out/path,identity

def _same(path,identity):
    if hr._identity(hb._regular(path))!=identity:
        raise hb.BundleError('cave candidate changed since planning')

def collect(out,*,execute=False,max_files=MAX_FILES,max_bytes=MAX_BYTES,now=None):
    if type(execute) is not bool:raise ValueError('execute must be boolean')
    if type(max_files) is not int or not 1<=max_files<=MAX_FILES:raise ValueError('invalid max_files')
    if type(max_bytes) is not int or not 1<=max_bytes<=MAX_BYTES:raise ValueError('invalid max_bytes')
    if os.name!='posix':raise RuntimeError('cave GC requires POSIX flock')
    import fcntl
    out=hb._owned(out)
    fd=os.open(out/'.build.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    removed_paths=[];removed_bytes=0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):raise hb.BundleError('invalid build lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state':'producer-busy','executed':False,'removed':0,'removedLogicalBytes':0}
        opened=os.fstat(fd);named=os.stat(out/'.build.lock',follow_symlinks=False)
        if (opened.st_dev,opened.st_ino)!=(named.st_dev,named.st_ino):raise hb.BundleError('build lock replaced')
        plan=retention.plan_retention(out,now=now)
        selected=[];size=0
        for item in plan['candidates']:
            path,identity=_candidate(out,item)
            if len(selected)>=max_files:break
            if size+identity['size']>max_bytes:break  # never skip an index then delete its referenced pairs
            selected.append((path,identity));size+=identity['size']
        for path,identity in selected:_same(path,identity)
        removed=removed_bytes=0;touched=set()
        try:
            if execute:
                for path,identity in selected:
                    # Revalidate directories as well as the file immediately before unlink.
                    hb._real_dir(path.parent.parent);hb._real_dir(path.parent)
                    _same(path,identity);os.unlink(path)
                    removed+=1;removed_bytes+=identity['size'];touched.add(path.parent)
                    removed_paths.append(path.relative_to(out).as_posix())
        finally:
            primary=sys.exc_info()[0] is not None;sync_error=None
            for folder in sorted(touched):
                try:hb._fsync_dir(folder)
                except OSError as error:sync_error=error
            if sync_error is not None and not primary:raise sync_error
        return {'state':'executed' if execute else 'dry-run','executed':execute,
                'planState':plan['state'],'selected':len(selected),'selectedLogicalBytes':size,
                'removed':removed,'removedLogicalBytes':removed_bytes,'reclaimableBytes':None,
                'candidateCount':plan['candidateCount'],'scanned':plan['scanned'],
                'candidatesTruncated':plan['candidatesTruncated'],
                'suppressedCandidates':plan['suppressedCandidates'],'unknown':plan['unknown'],
                'metadataBytes':plan['metadataBytes'],'protected':plan['protected'],
                'candidates':[p.relative_to(out).as_posix() for p,_ in selected]}
    except (OSError,ValueError) as error:
        if removed_paths:raise PartialCollectionError(error,removed_paths,removed_bytes) from error
        raise
    finally:os.close(fd)

def _bounded(limit):
    def parse(value):
        try:n=int(value)
        except ValueError:raise argparse.ArgumentTypeError('expected integer') from None
        if not 1<=n<=limit:raise argparse.ArgumentTypeError(f'expected 1..{limit}')
        return n
    return parse

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',required=True,type=Path);p.add_argument('--execute',action='store_true')
    p.add_argument('--max-files',type=_bounded(MAX_FILES),default=MAX_FILES);p.add_argument('--max-bytes',type=_bounded(MAX_BYTES),default=MAX_BYTES)
    a=p.parse_args(argv)
    try:result=collect(a.out,execute=a.execute,max_files=a.max_files,max_bytes=a.max_bytes)
    except PartialCollectionError as error:
        print(json.dumps(error.report,sort_keys=True),file=sys.stderr);return 1
    except (OSError,ValueError,RuntimeError) as error:
        print(('cave_gc: '+str(error)).replace('\n',' ')[:500],file=sys.stderr);return 1
    print(json.dumps(result,sort_keys=True));return 0

if __name__=='__main__':sys.exit(main())

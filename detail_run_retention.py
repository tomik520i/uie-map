"""Read-only retirement inspection for one completed detail run. No deletion API.

Caller must hold state/update.lock for a stable inspection. Queue+ledger stay as
audit records; only batches/plan/attempts/work/staged are possible scratch data.
A report is NOT authorization to delete: an executor must revalidate under locks.
"""
from pathlib import Path
import hashlib
import json
import re
import sqlite3
from contextlib import closing
import stat
from datetime import datetime, timezone

SCRATCH = ('batches', 'plan', 'attempts', 'work', 'staged')
MAX_FILES = 10000
MAX_BYTES = 1024**3


class RetentionError(ValueError):
    pass


def exact_path(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise RetentionError('Redirected path')
    return path


def read_small(path, limit=1024*1024):
    path = exact_path(path)
    with path.open('rb') as stream:
        raw = stream.read(limit+1)
    if len(raw)>limit:
        raise RetentionError('Oversized metadata')
    return raw


def inspect_run(state, name, store, *, now=None, grace_seconds=3600):
    if not isinstance(name,str) or not re.fullmatch(r'\d{8}-\d{6}',name):
        raise RetentionError('Invalid run name')
    if type(grace_seconds) is not int or grace_seconds<3600:
        raise RetentionError('Grace must be at least one hour')
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now,datetime) or now.tzinfo is None:
        raise RetentionError('Aware timestamp required')
    state=exact_path(state);store=exact_path(store);run=exact_path(state/'runs'/name)
    if run == store or store.is_relative_to(run) or run.is_relative_to(store):
        raise RetentionError('Store and working run overlap')
    db=exact_path(run/'queue.sqlite')
    if not db.is_file():raise RetentionError('Missing queue')
    with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as conn:
        jobs=conn.execute('SELECT id,state,result FROM jobs LIMIT 2').fetchall()
    if len(jobs)!=1 or jobs[0][1]!='succeeded':
        raise RetentionError('Run does not contain exactly one successful job')
    job_id,_,raw_result=jobs[0]
    if not isinstance(raw_result,str) or len(raw_result)>1024*1024:raise RetentionError('Invalid result')
    result=json.loads(raw_result)
    verified=result.get('verifiedSha256')
    if not isinstance(verified,str) or not re.fullmatch('[0-9a-f]{64}',verified):raise RetentionError('Invalid verification hash')
    ledger=json.loads(read_small(run/'ledger'/(verified+'.json')))
    for key,expected in [('format','UIE_ATTEMPT_PUBLICATION1'),('phase','published'),('jobId',job_id),
                         ('store',str(store)),('verifiedSha256',verified),('planSha256',result.get('planSha256')),
                         ('attempt',result.get('attempt'))]:
        if ledger.get(key)!=expected:raise RetentionError('Publication ledger does not match job')
    generation=ledger.get('generation')
    if not isinstance(generation,str) or not re.fullmatch('[0-9a-f]{64}',generation):raise RetentionError('Invalid generation')
    try:published=datetime.fromisoformat(ledger['publishedAt'].replace('Z','+00:00'))
    except (KeyError,TypeError,ValueError,AttributeError):raise RetentionError('Invalid publication time') from None
    if published.tzinfo is None:raise RetentionError('Publication time lacks timezone')
    age=(now-published).total_seconds()
    if age<grace_seconds:raise RetentionError('Publication is too recent or in the future')
    root=exact_path(store/'generations'/generation)
    complete=read_small(root/'COMPLETE.json',16*1024*1024)
    if hashlib.sha256(complete).hexdigest()!=generation:raise RetentionError('Published generation is damaged')
    inventory=json.loads(complete);files=inventory.get('files')
    if not isinstance(files,dict) or not 1<=len(files)<=MAX_FILES:raise RetentionError('Invalid generation inventory')
    if files.get('manifest.json')!=ledger.get('manifestSha256'):raise RetentionError('Manifest binding differs')
    published_bytes=0
    for relative,digest in files.items():
        if not isinstance(relative,str) or not re.fullmatch(r'[A-Za-z0-9_./-]+',relative):raise RetentionError('Invalid payload name')
        source=exact_path(root/relative)
        if not source.is_relative_to(root):raise RetentionError('Payload escapes generation')
        raw=read_small(source,128*1024*1024);published_bytes+=len(raw)
        if published_bytes>MAX_BYTES:raise RetentionError('Verification byte budget exceeded')
        if hashlib.sha256(raw).hexdigest()!=digest:raise RetentionError('Published payload is damaged')
    candidates=[];total=entries=0
    for directory in SCRATCH:
        folder=run/directory
        if not folder.exists() and not folder.is_symlink():continue
        folder=exact_path(folder)
        if not folder.is_dir():raise RetentionError('Scratch entry is not a directory')
        stack=[folder]
        while stack:
            current=stack.pop()
            for file in current.iterdir():
                entries+=1
                if entries>MAX_FILES:raise RetentionError('Scratch entry budget exceeded')
                info=file.lstat()
                if stat.S_ISDIR(info.st_mode):stack.append(file)
                elif stat.S_ISREG(info.st_mode):
                    total+=info.st_size
                    if total>MAX_BYTES:raise RetentionError('Scratch byte budget exceeded')
                    candidates.append({'file':file.relative_to(run).as_posix(),'bytes':info.st_size})
                else:raise RetentionError('Scratch contains symlink or special file')
    return {'run':str(run),'generation':generation,'logicalBytes':total,'files':candidates,
            'verifiedPublishedFiles':len(files),'keep':['queue.sqlite','ledger'],
            'inspectionOnly':True,'deleted':False}

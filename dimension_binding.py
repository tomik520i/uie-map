"""Persistent input identity for explicitly dimension-aware renderer stores.

Used by the experimental renderer route and publisher. Validation is read-only; explicit initialization
only accepts a fresh directory and never adopts existing render data. Linux init
uses its own lock; callers integrating this must keep the normal build/export lock
through validation AND subsequent writes. Paths are pinned, migration is explicit.
"""
import json
import os
from pathlib import Path
import stat
import tempfile
import world_layout as layout

NAME='.uie-dimension-binding.json'
LOCK='.dimension-binding.lock'
FORMAT='UIE_DIMENSION_BINDING1'
KEYS={'format','world','dimension','worldPath','regionPath','mode'}


class BindingError(ValueError):
    pass


def canonical(doc):return json.dumps(doc,sort_keys=True,separators=(',',':')).encode()


def validate(doc):
    if not isinstance(doc,dict) or set(doc)!=KEYS or doc['format']!=FORMAT:raise BindingError('Invalid binding schema')
    world=doc['world']
    if not isinstance(world,str) or not 1<=len(world)<=128 or world.strip()!=world or any(ord(c)<32 or ord(c)>126 for c in world):
        raise BindingError('Invalid world identity')
    key=doc['dimension']
    if not isinstance(key,str) or not (layout.numeric_key(key) or layout.CUSTOM.fullmatch(key)):
        raise BindingError('Invalid dimension identity')
    if doc['mode'] not in ('surface','caves','collisions'):raise BindingError('Invalid store mode')
    for name in ('worldPath','regionPath'):
        value=doc[name]
        if not isinstance(value,str) or not value or '\0' in value or not Path(value).is_absolute():raise BindingError('Invalid source path')
        if str(Path(value))!=value or '..' in Path(value).parts:raise BindingError('Noncanonical source path')
    root=Path(doc['worldPath']);region=Path(doc['regionPath'])
    if not region.is_relative_to(root) or region==root or region.name!='region':raise BindingError('Region directory escapes world')
    if len(canonical(doc))>8192:raise BindingError('Binding too large')
    return doc


def make_binding(*,world,world_id,dimension,dimension_path=None,mode):
    root=Path(world).absolute();layout.plain_directory(root);root=root.resolve()
    if dimension_path is None:
        if not layout.numeric_key(dimension):raise BindingError('Custom dimension needs explicit path')
        dimension_path='.' if dimension=='0' else 'DIM'+dimension
    found=layout.discover(root,[dict(key=dimension,path=dimension_path)])
    row=next(d for d in found['dimensions'] if d['key']==dimension)
    return validate(dict(format=FORMAT,world=world_id,dimension=dimension,worldPath=str(root),
                         regionPath=str(root/row['regionPath']),mode=mode))


def read_binding(out):
    path=Path(out)/NAME
    # Refuse links even on platforms without O_NOFOLLOW (validation is portable).
    if not stat.S_ISREG(path.lstat().st_mode):raise BindingError('Binding is not a regular file')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    try:
        before=os.fstat(fd);raw=os.read(fd,8193);after=os.fstat(fd);named=os.lstat(path)
        if len(raw)>8192:raise BindingError('Binding too large')
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
            raise BindingError('Binding changed while reading')
        if (after.st_dev,after.st_ino)!=(named.st_dev,named.st_ino):raise BindingError('Binding replaced while reading')
    finally:os.close(fd)
    doc=json.loads(raw);validate(doc)
    if canonical(doc)!=raw:raise BindingError('Binding JSON must be canonical')
    return doc


def ensure(out,expected,*,initialize=False):
    """Return unchanged/initialized. Never retargets a store or removes existing data."""
    validate(expected)
    if type(initialize)is not bool:raise BindingError('initialize must be bool')
    out=Path(out).absolute();layout.plain_directory(out)
    resolved=out.resolve()
    world=Path(expected['worldPath'])
    if resolved==world or resolved.is_relative_to(world) or world.is_relative_to(resolved):raise BindingError('Store overlaps source world')
    if not initialize:
        if read_binding(out)!=expected:raise BindingError('Store belongs to a different world, dimension, source path or mode')
        return dict(state='unchanged',initialized=False)
    if os.name!='posix':raise BindingError('Initialization requires POSIX locks')
    import fcntl
    fd=os.open(out/LOCK,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        opened=os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):raise BindingError('Lock is not regular')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise BindingError('Dimension initialization busy') from None
        named=os.lstat(out/LOCK)
        if (opened.st_dev,opened.st_ino)!=(named.st_dev,named.st_ino):raise BindingError('Lock replaced')
        if (out/NAME).exists() or (out/NAME).is_symlink():return ensure(out,expected)
        # A legacy output cannot be silently assigned a new source identity.
        for p in out.iterdir():
            if p.name not in (LOCK,'.build.lock','.uie-hires-bundle-owned') or not stat.S_ISREG(p.lstat().st_mode):
                raise BindingError('Unbound store is not fresh; refusing to adopt existing data')
        temp_fd,temp_name=tempfile.mkstemp(prefix='.dimension-binding-',dir=out)
        try:
            with os.fdopen(temp_fd,'wb') as stream:
                stream.write(canonical(expected));stream.flush();os.fsync(stream.fileno())
            os.link(temp_name,out/NAME,follow_symlinks=False)  # atomic, refuses overwrite
        finally:os.unlink(temp_name)
        directory=os.open(out,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        return dict(state='initialized',initialized=True)
    finally:os.close(fd)


def check_context(out,context):
    """Legacy stores are unchanged; bound stores require their explicit namespace."""
    out=Path(out)
    if not (out/NAME).exists() and not (out/NAME).is_symlink():return
    doc=read_binding(out)
    if (not isinstance(context,dict) or context.get('world')!=doc['world']
            or context.get('dimension')!=doc['dimension']
            or type(context.get('caves'))is not bool
            or doc['mode'] not in ('surface','caves')
            or context['caves']!=(doc['mode']=='caves')):
        raise BindingError('Publication context differs from the store source binding')

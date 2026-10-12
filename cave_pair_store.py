"""Private immutable pair objects, caller owns the build lock and output directory.

No current pointer or index is changed here. The manifest is created only after
both verified payloads exist. Interrupted writes may leave unreferenced objects,
never a reference to an incompletely written file. No automatic deletion.
"""
import hashlib
import gzip
import os
import stat
import tempfile
from pathlib import Path
import cave_pair as cp


def _directory(path):
    path.mkdir(exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):raise ValueError('Pair folder is not a real directory')


def _content(path,raw):
    """Create by atomic link; never replace an existing immutable object."""
    if os.path.lexists(path):
        st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size!=len(raw):raise ValueError('Invalid existing cave object')
        with path.open('rb') as stream:
            if stream.read(len(raw)+1)!=raw:raise ValueError('Existing cave object differs')
        return
    fd,tmp=tempfile.mkstemp(prefix='.cave-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        try:os.link(tmp,path)
        except FileExistsError:_content(path,raw)
        if os.name=='posix':
            directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        Path(tmp).unlink(missing_ok=True)


def store(out,geometry,caps,*,atlas_sha,renderer_identity):
    out=Path(out)
    if not out.is_absolute() or out.resolve()!=out or not stat.S_ISDIR(out.lstat().st_mode):
        raise ValueError('Pair output must be a real absolute directory')
    doc=cp.create(geometry,caps,atlas_sha=atlas_sha,renderer_identity=renderer_identity)
    raw=cp.canonical(doc);sha=hashlib.sha256(raw).hexdigest()
    root=out/'cave-data';_directory(root)
    for name in ('objects','caps','pairs'):_directory(root/name)
    _content(root/'objects'/f"{doc['geometrySha256']}.uih",geometry)
    # Address is the decoded SHA consumed by the browser. The HTTP layer must
    # validate decoded bytes then send this file with Content-Encoding: gzip.
    # Raw 256 KiB masks must not multiply into ~15 GiB for a whole world.
    compressed=gzip.compress(caps,compresslevel=9,mtime=0)
    compressed=compressed[:9]+b'\xff'+compressed[10:]
    _content(root/'caps'/f"{doc['capSha256']}.uic.gz",compressed)
    _content(root/'pairs'/f'{sha}.json',raw)
    return sha,doc


def publish_geometry_alias(out,origin,geometry_sha):
    """Atomically expose the immutable geometry inode to the legacy index builder.

    Caller has just created/verified this object under its exclusive build lock.
    Future render writes replace this alias instead of modifying shared bytes.
    """
    out=Path(out)
    if not cp.SHA.fullmatch(geometry_sha) or len(origin)!=2 or any(type(v) is not int or v%64 for v in origin):
        raise ValueError('Invalid cave geometry alias')
    folder=out/'tiles'
    if folder.resolve()!=folder or not stat.S_ISDIR(folder.lstat().st_mode):raise ValueError('Invalid tile folder')
    source=out/'cave-data/objects'/f'{geometry_sha}.uih'
    if source.resolve()!=source or not stat.S_ISREG(source.lstat().st_mode):raise ValueError('Invalid immutable geometry')
    target=folder/f'{origin[0]//64}_{origin[1]//64}.uih'
    fd,tmp=tempfile.mkstemp(prefix='.cave-alias-',dir=folder);os.close(fd);Path(tmp).unlink()
    try:
        os.link(source,tmp);os.replace(tmp,target)
    finally:
        Path(tmp).unlink(missing_ok=True)

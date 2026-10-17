"""Read-only inventory of Minecraft 1.7.10 dimension directories.

Discovery never selects dimensions for rendering and does not validate the world
format or block registry. Nonstandard paths require explicit x:name mappings.
No symlinks/junctions below the world are followed; no world files are written.
"""
import argparse
import json
from pathlib import Path
import re
import stat

INTEGER = re.compile(r'(?:0|-[1-9][0-9]*|[1-9][0-9]*)\Z')
CUSTOM = re.compile(r'x:[A-Za-z0-9][A-Za-z0-9_.-]{0,125}\Z')
REGION = re.compile(r'r\.(0|-[1-9][0-9]*|[1-9][0-9]*)\.(0|-[1-9][0-9]*|[1-9][0-9]*)\.mca\Z')


class LayoutError(ValueError):
    pass


def numeric_key(value):
    return (isinstance(value,str) and len(value)<=11 and INTEGER.fullmatch(value) is not None
            and -(2**31)<=int(value)<2**31)


def plain_directory(path, *, optional=False):
    try:mode=path.lstat().st_mode
    except FileNotFoundError:
        if optional:return False
        raise LayoutError('Missing directory: '+str(path)) from None
    if stat.S_ISLNK(mode) or getattr(path,'is_junction',lambda:False)():
        raise LayoutError('Linked directory is not allowed: '+str(path))
    if not stat.S_ISDIR(mode):raise LayoutError('Expected a directory: '+str(path))
    return True


def relative_parts(value):
    if (not isinstance(value,str) or not value or '\\' in value or ':' in value
            or value.startswith('/') or '\0' in value):
        raise LayoutError('Dimension path must be relative, using forward slashes')
    if value=='.':return ()
    parts=value.split('/')
    if any(part in ('','.', '..') for part in parts):raise LayoutError('Noncanonical dimension path')
    return tuple(parts)


def discover(world, custom=None, *, max_entries=100000):
    """Return {dimensions, warnings}; custom is a list of {key, path} records.

    Paths name dimension roots, not region directories. Empty DIM directories are
    reported with zero regions. Unknown DIM names (including archives) are warnings,
    never automatic candidates. Budget covers directory entries across the inventory.
    Directory inventory is a point-in-time observation, not a stable render snapshot.
    """
    if type(max_entries)is not int or not 1<=max_entries<=1000000:raise LayoutError('Invalid inventory limit')
    root=Path(world).absolute();plain_directory(root);root=root.resolve()
    if custom is None:custom=[]
    if not isinstance(custom,list) or len(custom)>1024:raise LayoutError('Invalid dimension mappings')
    candidates={'0':('.', 'standard')};warnings=[];count=0
    def entries(path):
        nonlocal count
        result=[]
        for entry in path.iterdir():
            count+=1
            if count>max_entries:raise LayoutError('Dimension inventory entry limit exceeded')
            result.append(entry)
        return sorted(result,key=lambda p:p.name)
    for p in entries(root):
        if not p.name.startswith('DIM'):continue
        key=p.name[3:]
        if not numeric_key(key) or key=='0':
            warnings.append(dict(path=p.name,reason='Noncanonical or reserved DIM name; ignored'))
            continue
        plain_directory(p)
        candidates[key]=(p.name,'standard')
    seen=set()
    for item in custom:
        if not isinstance(item,dict) or set(item)!={'key','path'}:raise LayoutError('Expected key/path mapping')
        key=item['key'];value=item['path']
        if not isinstance(key,str) or not (numeric_key(key) or CUSTOM.fullmatch(key)):
            raise LayoutError('Invalid dimension key')
        if key in seen:raise LayoutError('Duplicate dimension key: '+key)
        seen.add(key);relative_parts(value)
        if numeric_key(key) and value!=('.' if key=='0' else 'DIM'+key):
            raise LayoutError('Nonstandard paths require an x:name key')
        candidates[key]=(value,'explicit')
    dimensions=[];used=set()
    for key,(value,source) in sorted(candidates.items(),key=lambda item:(not numeric_key(item[0]),int(item[0]) if numeric_key(item[0]) else item[0])):
        p=root
        for part in relative_parts(value):p=p/part;plain_directory(p)
        # Reject aliases, including case-insensitive aliases on Windows.
        resolved=p.resolve()
        if not resolved.is_relative_to(root):raise LayoutError('Dimension escapes world')
        if resolved in used:raise LayoutError('Duplicate dimension path: '+value)
        used.add(resolved);region=p/'region';exists=plain_directory(region,optional=True);regions=0
        if exists:
            for f in entries(region):
                if not REGION.fullmatch(f.name):
                    warnings.append(dict(path=f.relative_to(root).as_posix(),reason='Noncanonical region entry; ignored'))
                    continue
                if not stat.S_ISREG(f.lstat().st_mode):raise LayoutError('Region is not a regular file: '+str(f))
                regions+=1
        dimensions.append(dict(key=key,path=value,regionPath=region.relative_to(root).as_posix(),
                               regions=regions,regionDirectoryExists=exists,source=source))
    return dict(dimensions=dimensions,warnings=warnings)


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('world',type=Path)
    ap.add_argument('--dimension',action='append',default=[],metavar='KEY=PATH',help='Explicit dimension-root mapping')
    a=ap.parse_args(argv);custom=[]
    for value in a.dimension:
        key,sep,path=value.partition('=')
        if not sep:ap.error('--dimension requires KEY=PATH')
        custom.append(dict(key=key,path=path))
    try:result=discover(a.world,custom)
    except (OSError,LayoutError) as e:ap.exit(2,str(e)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())

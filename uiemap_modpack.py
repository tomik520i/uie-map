"""Read-only inventory of Minecraft mod JAR metadata and texture entry names.

Does not execute Java, extract archives, start Minecraft, or produce a render catalog.
By default texture bytes are not read. Opt-in block PNG validation never proves models.
"""
import argparse
from collections import Counter,defaultdict
import json
import os
from pathlib import Path,PurePosixPath
import stat
import zipfile

MAX_JARS=1024
MAX_JAR_BYTES=1024**3
MAX_ENTRIES=100000
MAX_TOTAL_ENTRIES=500000
MAX_INFO_BYTES=512*1024


def metadata(raw):
    doc=json.loads(raw.decode('utf-8-sig'))
    if isinstance(doc,dict):doc=doc.get('modList',[doc])
    if not isinstance(doc,list) or len(doc)>512:raise ValueError('invalid mcmod.info list')
    rows=[]
    for item in doc:
        if not isinstance(item,dict):raise ValueError('invalid mcmod.info entry')
        row={k:item[k][:512] for k in ('modid','name','version','mcversion') if isinstance(item.get(k),str)}
        if row:rows.append(row)
    return rows


def inspect_jar(path, *, verify_block_textures=False, texture_budget=None):
    path=Path(path)
    if path.is_symlink():raise ValueError('linked archive refused')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 1<=before.st_size<=MAX_JAR_BYTES:
            raise ValueError('archive size/type outside inventory limits')
        textures=[];mods=[];warnings=[];seen=set()
        with zipfile.ZipFile(stream) as archive:
            entries=archive.infolist()
            if len(entries)>MAX_ENTRIES:raise ValueError('archive entry limit exceeded')
            for info in entries:
                name=info.orig_filename
                if len(name)>4096 or '\\' in name or '\x00' in name or name.startswith('/') or any(v in ('..','') for v in name.rstrip('/').split('/')):
                    raise ValueError('unsafe archive entry name')
                if name in seen:raise ValueError('duplicate archive entry name')
                seen.add(name)
                if stat.S_ISLNK(info.external_attr>>16):raise ValueError('archive symlink entry refused')
                if name=='mcmod.info':
                    if info.flag_bits&1 or info.file_size>MAX_INFO_BYTES:
                        warnings.append('mcmod.info is encrypted or exceeds size limit');continue
                    try:
                        with archive.open(info) as source:raw=source.read(MAX_INFO_BYTES+1)
                        if len(raw)>MAX_INFO_BYTES:raise ValueError('metadata limit exceeded')
                        mods=metadata(raw)
                    except (ValueError,UnicodeError,zipfile.BadZipFile,RuntimeError,RecursionError):
                        warnings.append('mcmod.info could not be decoded')
                parts=PurePosixPath(name).parts
                if len(parts)>=5 and parts[0]=='assets' and parts[2]=='textures' and name.lower().endswith('.png'):
                    textures.append(dict(resource=parts[1]+':'+('/'.join(parts[3:]))[:-4],entry=name,
                                         bytes=info.file_size,block=parts[3] in ('blocks','block')))
            if verify_block_textures:
                import modpack_textures
                if texture_budget is None:texture_budget={'bytes':0}
                for texture in textures:
                    if texture['block']:
                        texture['validation']=modpack_textures.verify(archive,archive.getinfo(texture['entry']),seen,texture_budget)
        after=os.fstat(stream.fileno());named=path.lstat()
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        if identity(before)!=identity(after) or identity(after)!=identity(named) or stat.S_ISLNK(named.st_mode):
            raise ValueError('archive changed during inventory')
    if not mods:warnings.append('No usable mcmod.info; mod identity is unknown')
    return dict(file=path.name,archiveBytes=before.st_size,entries=len(entries),mods=mods,
                textureCandidates=len(textures),blockTextureCandidates=sum(t['block'] for t in textures),
                namespaces=dict(sorted(Counter(t['resource'].split(':',1)[0] for t in textures).items())),
                warnings=warnings),textures


def inventory(mods,*,include_textures=False,world=None,include_blocks=False,namespace_map=None,verify_block_textures=False):
    if include_blocks and world is None:raise ValueError('--include-blocks requires --world')
    if namespace_map is not None and world is None:raise ValueError('--namespace-map requires --world')
    root=Path(mods).absolute()
    if root.resolve()!=root or not root.is_dir():raise ValueError('mods must be an existing non-linked directory')
    paths=sorted(p for p in root.iterdir() if p.suffix.lower()=='.jar')
    if len(paths)>MAX_JARS:raise ValueError('archive count limit exceeded')
    rows=[];errors=[];resources=defaultdict(set);block_resources=defaultdict(set);total=0;texture_budget={'bytes':0}
    for path in paths:
        try:
            if verify_block_textures:
                row,textures=inspect_jar(path,verify_block_textures=True,texture_budget=texture_budget)
                import modpack_textures
                row['blockTextureValidation']=modpack_textures.summary(textures)
            else:row,textures=inspect_jar(path)
            total+=row['entries']
            if total>MAX_TOTAL_ENTRIES:raise ValueError('total entry limit exceeded')
            for t in textures:
                resources[t['resource']].add(path.name)
                if t['block']:block_resources[t['resource']].add(path.name)
            if include_textures:row['textures']=textures
            rows.append(row)
        except (OSError,ValueError,zipfile.BadZipFile,zipfile.LargeZipFile) as error:
            errors.append(dict(file=path.name,error=str(error)[:300]))
            if total>MAX_TOTAL_ENTRIES:break
    result=dict(format='UIEMAP_MODPACK_INVENTORY1',readOnly=True,state='partial' if errors else 'inventory-complete',
                archivesFound=len(paths),archivesInspected=len(rows),archives=rows,errors=errors,
                textureConflicts=[dict(resource=k,archives=sorted(v)) for k,v in sorted(resources.items()) if len(v)>1],
                catalogReady=False,texturePayloadsVerified=False,
                limitations=['JAR entry names and bounded mcmod.info only; no Java or Minecraft execution.',
                    'Texture candidates are not decoded or CRC-verified. Conflicts have no chosen load order.',
                    'Registry IDs, metadata variants, faces and Java-rendered models still need a catalog adapter.',
                    'Only top-level .jar files are inspected; nested archives and configuration files are not imported.'])
    if verify_block_textures:
        keys=('checked','verified','invalid','unsupported','limited','staticSquareCandidates','animationMetadataPresent')
        counts={key:sum(row['blockTextureValidation'][key] for row in rows) for key in keys}
        complete=not errors and counts['invalid']==counts['unsupported']==counts['limited']==0
        result['blockTextureValidation']=dict(counts,complete=complete,reservedBytes=texture_budget['bytes'])
        # The legacy field covers ALL textures, including items/GUI, so stays false.
        if not complete:result['state']='partial'
        result['limitations'][0]='JAR metadata and bounded block PNG validation; no Java or Minecraft execution.'
        result['limitations'][1]='Only block PNG payloads are checked. No animation playback, face mapping or chosen conflict precedence.'
    if world is not None:
        import modpack_registry
        result['registry']=modpack_registry.compare(modpack_registry.read_registry(world),rows,block_resources,
            include_blocks=include_blocks,archive_scan_complete=not errors,
            namespace_map=modpack_registry.load_namespace_map(namespace_map) if namespace_map is not None else None)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--mods',required=True,type=Path)
    parser.add_argument('--include-textures',action='store_true')
    parser.add_argument('--world',type=Path,help='Optional world root containing Forge level.dat')
    parser.add_argument('--include-blocks',action='store_true',help='Include registered IDs and texture name candidates; requires --world')
    parser.add_argument('--namespace-map',type=Path,help='Explicit JSON mapping of registry namespaces to texture namespaces')
    parser.add_argument('--verify-block-textures',action='store_true',help='Read and validate bounded block PNG payloads without extraction')
    args=parser.parse_args(argv)
    try:doc=inventory(args.mods,include_textures=args.include_textures,world=args.world,include_blocks=args.include_blocks,namespace_map=args.namespace_map,verify_block_textures=args.verify_block_textures)
    except (OSError,ValueError) as error:parser.exit(2,str(error)+'\n')
    print(json.dumps(doc,ensure_ascii=False,indent=2));return 0 if doc['state']=='inventory-complete' else 1

if __name__=='__main__':raise SystemExit(main())

"""Compare a Forge block registry with archive names; never infer render support."""
from collections import defaultdict
import hashlib,json,os,stat,re
from pathlib import Path
import reader

MAX_LEVEL_BYTES=16*1024*1024


def read_registry(world):
    root=Path(world).absolute()
    if root.resolve()!=root or not root.is_dir():raise ValueError('world must be an existing non-linked directory')
    path=root/'level.dat'
    if path.is_symlink():raise ValueError('linked level.dat refused')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 1<=before.st_size<=MAX_LEVEL_BYTES:
            raise ValueError('level.dat size/type outside inventory limits')
        raw=stream.read(MAX_LEVEL_BYTES+1);after=os.fstat(stream.fileno());named=path.lstat()
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        if len(raw)!=before.st_size or identity(before)!=identity(after) or identity(after)!=identity(named) or stat.S_ISLNK(named.st_mode):
            raise ValueError('level.dat changed during inventory')
    return dict(reader.parse_fml_registry(reader.parse_level_dat(raw)).blocks)


def load_namespace_map(path):
    path=Path(path)
    if path.is_symlink():raise ValueError('linked namespace map refused')
    with path.open('rb') as stream:raw=stream.read(65537)
    if len(raw)>65536:raise ValueError('namespace map exceeds 64 KiB')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate namespace mapping')
            result[key]=value
        return result
    doc=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=unique)
    if not isinstance(doc,dict) or len(doc)>1024:raise ValueError('namespace map must be an object of at most 1024 entries')
    for source,target in doc.items():
        if not isinstance(source,str) or not 1<=len(source)<=256 or ':' in source or not isinstance(target,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',target):
            raise ValueError('invalid namespace mapping')
    return doc


def compare(registry,archives,block_resources,*,include_blocks=False,archive_scan_complete=True,namespace_map=None):
    namespace_map=namespace_map or {}
    known={name.partition(':')[0] for name in registry.values() if ':' in name}
    if set(namespace_map)-known:raise ValueError('namespace map contains names absent from the block registry')
    grouped=defaultdict(list);namespace_resources=defaultdict(set);namespace_archives=defaultdict(set)
    for resource,providers in block_resources.items():
        namespace=resource.split(':',1)[0]
        namespace_resources[namespace].add(resource);namespace_archives[namespace].update(providers)
    declared=defaultdict(set)
    for archive in archives:
        for mod in archive['mods']:
            if mod.get('modid'):declared[mod['modid']].add(archive['file'])
    total_candidates=0
    for block_id,name in sorted(registry.items()):
        namespace,separator,local=name.partition(':')
        if not separator:namespace='';local=name
        candidates=[];texture_namespace=namespace_map.get(namespace,namespace)
        for key in (texture_namespace+':blocks/'+local,texture_namespace+':block/'+local):
            if key in block_resources:
                candidates.append(dict(resource=key,archives=sorted(block_resources[key])))
        total_candidates+=bool(candidates)
        grouped[namespace].append(dict(id=block_id,registryName=name,nameCandidates=candidates,
                                       mappingVerified=False))
    rows=[]
    for namespace,blocks in sorted(grouped.items()):
        texture_namespace=namespace_map.get(namespace,namespace)
        exact=namespace_resources.get(texture_namespace,set())
        row=dict(namespace=namespace,registeredBlocks=len(blocks),blocksWithNameCandidates=sum(bool(b['nameCandidates']) for b in blocks),
                 textureNamespace=texture_namespace,namespaceMappingExplicit=namespace in namespace_map,
                 exactTextureNamespaceFound=bool(exact),blockTextureCandidates=len(exact),
                 textureArchives=sorted(namespace_archives.get(texture_namespace,set())),
                 declaredModArchives=sorted(declared.get(namespace,set())),
                 caseVariantTextureNamespaces=sorted(n for n in namespace_resources if n!=texture_namespace and n.casefold()==texture_namespace.casefold()),
                 reviewNeeded=not bool(exact))
        if include_blocks:row['blocks']=blocks
        rows.append(row)
    sha=hashlib.sha256(json.dumps(sorted(registry.items()),separators=(',',':')).encode()).hexdigest()
    return dict(format='UIEMAP_MODPACK_REGISTRY1',registrySha256=sha,registeredBlocks=len(registry),
                blocksWithNameCandidates=total_candidates,namespaces=rows,archiveScanComplete=archive_scan_complete,
                mappingVerified=False,
                limitations=['Registered does not mean placed in the world; chunks were not scanned.',
                    'Matching names/namespaces only suggest candidates, not faces, metadata variants or geometry.',
                    'Different asset domains and absent mcmod.info are valid mod patterns; missing matches do not prove a missing mod.',
                    'Vanilla textures may come from the Minecraft archive, which is normally outside mods.',
                    'Incomplete archive scans can omit candidates; case variants are reported without assigning them.'])

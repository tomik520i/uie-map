"""Offline immutable generation assembly with one atomic revision pointer.

Input batches already have UIE2-bound blockInfo. All referenced geometry/lookup files are validated,
copied and hashed before current.json changes. A failed build keeps the old pointer. This is not a
live-world snapshot, a renderer, an incremental inventory or a whole-world paged browser index.
The caller binds each batch to its expected input SHA and supplies one world/dimension/pipeline.
"""
from pathlib import Path, PurePosixPath
import hashlib,json,math,os,re,uuid,shutil,gzip,errno


class PublicationError(ValueError): pass

def canonical(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)+'\n').encode()

def digest(raw): return hashlib.sha256(raw).hexdigest()

def sha(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
        raise PublicationError('Invalid SHA256')
    return value

def integer(value): return isinstance(value,int) and not isinstance(value,bool)

def finite(value): return type(value) in (int,float) and math.isfinite(value)

def read_relative(root,name):
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_./-]+',name):
        raise PublicationError('Unsafe relative file')
    path=PurePosixPath(name)
    if path.is_absolute() or any(p in ('..','.') for p in name.split('/')):
        raise PublicationError('Unsafe relative file')
    resolved=(root/name).resolve()
    if not resolved.is_relative_to(root):raise PublicationError('File escapes batch')
    limit=128*1024*1024
    with resolved.open('rb') as stream:
        # Read the actual size (+1 to detect growth): read(limit+1) preallocates the whole limit on Windows.
        size=os.fstat(stream.fileno()).st_size
        if size>limit:raise PublicationError('Oversized batch file')
        raw=stream.read(size+1)
        if len(raw)>size:raw+=stream.read(limit+1-len(raw))
    if len(raw)>128*1024*1024:raise PublicationError('Oversized batch file')
    return raw

def validate_mesh(doc):
    if not isinstance(doc,dict):raise PublicationError('Invalid mesh')
    parts=doc.get('transparentParts',[])
    if not isinstance(parts,list):raise PublicationError('Invalid transparent parts')
    for part in [doc,*parts]:
        if not isinstance(part,dict):raise PublicationError('Invalid mesh part')
        if part is not doc and 'transparentParts' in part:raise PublicationError('Nested mesh parts')
        for name in ('positions','uvs','colors','indices'):
            if not isinstance(part.get(name),list):raise PublicationError('Missing mesh arrays')
        pos,uv,col,indices=[part[k] for k in ('positions','uvs','colors','indices')]
        n=len(pos)//3
        if len(pos)%3 or len(uv)!=n*2 or len(col)!=n*3 or len(indices)%3:
            raise PublicationError('Mesh array lengths differ')
        if not all(finite(v) for arr in (pos,uv,col) for v in arr):raise PublicationError('Nonfinite mesh')
        if not all(integer(i) and 0<=i<n for i in indices):raise PublicationError('Invalid mesh indices')
        if 'transparent' in part and type(part['transparent']) is not bool:raise PublicationError('Invalid transparency')

def validate_blocks(doc,cx,cz):
    if not isinstance(doc,dict) or doc.get('format')!='UIE_BLOCKS1':raise PublicationError('Invalid block document')
    if [doc.get(k) for k in ('x','z','width','depth','height')]!=[cx*16,cz*16,16,16,256]:
        raise PublicationError('Block document coordinates differ')
    registry=doc.get('registry');runs=doc.get('runs')
    if not isinstance(registry,dict) or not isinstance(runs,list):raise PublicationError('Invalid block data')
    for key,name in registry.items():
        if not isinstance(key,str) or not re.fullmatch(r'0|[1-9][0-9]*',key) or int(key)>65535 or not isinstance(name,str) or not name:
            raise PublicationError('Invalid block registry')
    total=0
    for run in runs:
        if not isinstance(run,list) or len(run)!=3 or not all(integer(v) for v in run):raise PublicationError('Invalid block run')
        n,b,m=run
        if n<1 or not 0<=b<=65535 or not 0<=m<=15 or not isinstance(registry.get(str(b)),str) or not registry[str(b)]:
            raise PublicationError('Invalid block run value')
        total+=n
        if total>65536:raise PublicationError('Block runs overflow')
    if total!=65536:raise PublicationError('Block runs incomplete')

STAGED_SOURCE_FILE='staged-source.json'
OBSERVED_STABLE='observed-stable-files'

def staged_source_record(ref,raw,expected_snapshot):
    """Validate a UIE_STAGED_SOURCE1 sidecar (staged_batch.py) for one batch; return its COMPLETE record.
    The policy is carried as-is: observed-stable-files and never an atomic/offline-consistent claim."""
    if not isinstance(ref,dict) or set(ref)!={'file','sha256'} or ref['file']!=STAGED_SOURCE_FILE:
        raise PublicationError('Invalid stagedSource spec')
    sha(ref['sha256'])
    if digest(raw)!=ref['sha256']:raise PublicationError('Staged source sidecar hash mismatch')
    def constant(name):raise PublicationError('Staged source sidecar has non-finite numbers')
    try:doc=json.loads(raw.decode('utf-8'),parse_constant=constant)
    except (UnicodeDecodeError,json.JSONDecodeError):raise PublicationError('Staged source sidecar is not JSON') from None
    from datetime import datetime
    def stamp(value):
        try:dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        except (AttributeError,ValueError):raise PublicationError('Invalid staged source interval') from None
        if dt.tzinfo is None:raise PublicationError('Staged source interval needs timezone')
        return dt
    try:
        policy,tile,batch=doc['policy'],doc['stagedTile'],doc['batch']
        valid=(doc['format']=='UIE_STAGED_SOURCE1' and policy['acceptedConsistency']==OBSERVED_STABLE
               and policy['atomicWorldSnapshot'] is False and tile['consistency']==OBSERVED_STABLE
               and tile['atomicWorldSnapshot'] is False and isinstance(doc['warning'],str) and doc['warning'].strip())
        snapshot_bound=batch['snapshotSha256']==expected_snapshot and batch['files']['snapshot.bin']==expected_snapshot
        interval=tile['sourceInterval']
    except (KeyError,TypeError):raise PublicationError('Staged source sidecar is malformed') from None
    if not valid:raise PublicationError('Staged source policy is not observed-stable-files and non-atomic')
    if not snapshot_bound:raise PublicationError('Staged source sidecar belongs to another snapshot')
    if not isinstance(interval,dict) or set(interval)!={'startUtc','endUtc'} or stamp(interval['startUtc'])>stamp(interval['endUtc']):
        raise PublicationError('Invalid staged source interval')
    return {'snapshotSha256':expected_snapshot,'file':f'provenance/{expected_snapshot}-{ref["sha256"]}.json',
            'sha256':ref['sha256'],'consistency':OBSERVED_STABLE,'atomicWorldSnapshot':False}

def _write(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())

def _sync_dir(path):
    if os.name=='posix':
        fd=os.open(path,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)

CAPTURE_MANIFEST='capture-manifest.json'
ATLAS_LAYOUT='atlas-layout.json'

def _area_tile(tile,root,source,m,local,ref,sidecar):
    """Bind one area-record tile to its batch (tracked publication only). Returns (atlas sha, layout identity).

    capture-manifest.json is the ORIGINAL verified capture manifest (its hash is tile.manifestSha256); the
    working manifest.json may differ from it only by the added blockInfo. atlas-layout.json is the verified
    layout, bound to the capture manifest and to the actual atlas PNG."""
    import atlas_identity  # only for tracked publications
    def aux(name):
        try:return source(name)
        except OSError:raise PublicationError(f'Tracked publication needs {name} in every batch') from None
    capture_raw=aux(CAPTURE_MANIFEST)
    if digest(capture_raw)!=tile['manifestSha256']:raise PublicationError('Capture manifest does not match the area record')
    try:capture=json.loads(capture_raw)
    except ValueError:raise PublicationError('Capture manifest is not JSON') from None
    if not isinstance(capture,dict) or 'blockInfo' in capture or canonical({k:v for k,v in m.items() if k!='blockInfo'})!=canonical(capture):
        raise PublicationError('Working manifest differs from the capture manifest beyond the added blockInfo')
    coverage=capture.get('coverage')
    if not isinstance(coverage,dict):raise PublicationError('Capture manifest coverage is malformed')
    expected={'renderedBlocks':coverage.get('renderedBlocks'),'unsupportedBlocks':coverage.get('unsupportedBlocks'),
              'emptyBlocks':coverage.get('emptyBlocks',0),'unsupportedNames':sorted(set(coverage.get('unsupportedNames',[])))}
    if canonical(tile['bounds'])!=canonical(capture.get('bounds')) or canonical(tile['coverage'])!=canonical(expected) \
            or tile['generatedAt']!=capture.get('generatedAt'):
        raise PublicationError('Area record tile differs from its capture manifest (bounds, coverage or capture time)')
    if {tuple(c) for c in tile['present']}!=local:raise PublicationError('Area record present chunks differ from the batch')
    layout_raw=aux(ATLAS_LAYOUT)
    if capture.get('atlasLayout')!=ATLAS_LAYOUT or digest(layout_raw)!=capture.get('atlasLayoutSha256'):
        raise PublicationError('Atlas layout is not the one bound by the capture manifest')
    try:layout,atlas_sha=atlas_identity.load_layout(root/m['atlas'],root/ATLAS_LAYOUT)
    except (ValueError,OSError) as error:raise PublicationError(f'Atlas layout binding failed: {error}') from None
    if ref is None:
        if tile['stagedSourceSha256'] is not None or tile['sourceInterval'] is not None:
            raise PublicationError('Area record claims a staged source the batch does not supply')
    else:
        # staged_source_record already accepted the sidecar, so its interval exists and is valid.
        interval=json.loads(sidecar)['stagedTile']['sourceInterval']
        if tile['stagedSourceSha256']!=ref['sha256'] or tile['sourceInterval'] is None \
                or canonical(tile['sourceInterval'])!=canonical(interval):
            raise PublicationError('Area record staged source differs from the batch sidecar')
    return atlas_sha,atlas_identity.layout_identity(layout)

def _utc(value):
    from datetime import datetime
    return datetime.fromisoformat(value.replace('Z','+00:00'))

def _carry_plan(store,generation,pipeline_key,area):
    """Plan a carry-forward from the tracked base generation (the current, CAS-checked revision).

    Returns (base root, base inventory files, carried tiles, kept area records, superseded tile snapshots,
    carried source policies). Every base tile not overlapping the new area is carried whole; a tile whose core
    is exactly a new tile's core is replaced (only by a strictly later observed source); anything else fails."""
    import area_records
    relative='generations/'+generation
    inventory_raw=read_relative(store,relative+'/COMPLETE.json')
    if digest(inventory_raw)!=generation:raise PublicationError('Carry-forward base inventory is damaged')
    base=json.loads(inventory_raw)
    if not isinstance(base,dict) or base.get('format')!='UIE_GENERATION1' or not isinstance(base.get('files'),dict):
        raise PublicationError('Invalid carry-forward base inventory')
    if base.get('pipelineKey')!=pipeline_key:
        raise PublicationError('Carry-forward base has another pipeline; a full rebuild is required')
    records=base.get('areas')
    if not isinstance(records,list) or not records:
        raise PublicationError('Carry-forward base is not area-tracked; publish a full tracked candidate first')
    validated=[]
    for record in records:
        try:checked=area_records.validate_area_record(record)
        except area_records.AreaRecordError as error:raise PublicationError(f'Invalid base area record: {error}') from None
        if canonical(checked)!=canonical(record):raise PublicationError('Base area record is not canonical')
        for k in ('pipelineKey','atlasSha256','layoutIdentitySha256'):
            if checked[k]!=area[k]:raise PublicationError(f'Carry-forward base differs in {k}; a full rebuild is required')
        if checked['bakedLighting'] is not area['bakedLighting']:
            raise PublicationError('Carry-forward base differs in bakedLighting; a full rebuild is required')
        validated.append(checked)
    tiles={}
    for index,record in enumerate(validated):
        for tile in record['tiles']:
            if tile['snapshotSha256'] in tiles:raise PublicationError('Base area records repeat a snapshot')
            tiles[tile['snapshotSha256']]=(index,tile)
    superseded=base.get('supersededTiles',[])
    if not isinstance(superseded,list) or not all(isinstance(s,str) for s in superseded) \
            or len(set(superseded))!=len(superseded) or any(s not in tiles for s in superseded):
        raise PublicationError('Invalid base supersededTiles')
    active={s:v for s,v in tiles.items() if s not in superseded}
    if {index for index,_ in active.values()}!=set(range(len(validated))):
        raise PublicationError('Base keeps an area record without an active tile')
    if base.get('snapshots')!=sorted(active):raise PublicationError('Base snapshots differ from its active tiles')
    owner={}
    for snapshot,(_,tile) in active.items():
        for c in tile['coreChunks']:
            if tuple(c) in owner:raise PublicationError('Base active tiles overlap')
            owner[tuple(c)]=snapshot
    files=base['files']
    if not isinstance(files.get('manifest.json'),str) or not isinstance(files.get('atlas.png'),str):
        raise PublicationError('Invalid carry-forward base inventory')
    manifest_raw=read_relative(store,relative+'/manifest.json')
    if digest(manifest_raw)!=files['manifest.json']:raise PublicationError('Carry-forward base manifest is damaged')
    manifest=json.loads(manifest_raw)
    present={tuple(c) for _,tile in active.values() for c in tile['present']}
    def keys(entries):
        result=[(e.get('x'),e.get('z')) for e in entries if isinstance(e,dict)]
        if len(result)!=len(entries) or len(set(result))!=len(result):raise PublicationError('Invalid base manifest chunks')
        return set(result)
    if keys(manifest.get('chunks',[]))!=present or keys(manifest.get('blockInfo',{}).get('chunks',[]))!=present:
        raise PublicationError('Base manifest chunks differ from its active area tiles')
    if files['atlas.png']!=area['atlasSha256']:raise PublicationError('Carry-forward base atlas differs; a full rebuild is required')
    new_core={}
    for tile in area['tiles']:
        if tile['snapshotSha256'] in active:raise PublicationError('New area repeats an active base snapshot')
        for c in tile['coreChunks']:new_core[tuple(c)]=tile
    carried=[];replaced=set()
    for snapshot,(index,tile) in sorted(active.items()):
        core={tuple(c) for c in tile['coreChunks']}
        hits={id(new_core[c]):new_core[c] for c in core if c in new_core}
        if not hits:carried.append((index,tile));continue
        new=next(iter(hits.values()))
        if len(hits)!=1 or {tuple(c) for c in new['coreChunks']}!=core:
            raise PublicationError('New area partially overlaps a base tile; only whole tiles can be replaced')
        old_interval,new_interval=tile['sourceInterval'],new['sourceInterval']
        if old_interval is None or new_interval is None:
            raise PublicationError('Replacing a tile needs observed source intervals on both sides')
        if _utc(new_interval['startUtc'])<_utc(old_interval['endUtc']):
            raise PublicationError('Replacement source was not observed after the replaced tile')
        replaced.add(snapshot)
    kept=sorted({index for index,_ in carried})
    carried_snapshots={tile['snapshotSha256'] for _,tile in carried}
    kept_records=[records[i] for i in kept]
    superseded_out=sorted(s for s,(index,_) in tiles.items() if index in kept and s not in carried_snapshots)
    policies=[]
    base_policies=base.get('sourcePolicies',[])
    if not isinstance(base_policies,list):raise PublicationError('Invalid base sourcePolicies')
    for _,tile in carried:
        matches=[p for p in base_policies if isinstance(p,dict) and p.get('snapshotSha256')==tile['snapshotSha256']]
        if tile['stagedSourceSha256'] is None:
            if matches:raise PublicationError('Base source policy without a staged-source tile')
            continue
        if len(matches)!=1 or matches[0].get('sha256')!=tile['stagedSourceSha256'] or matches[0].get('file') not in files:
            raise PublicationError('Base source policy differs from its area tile')
        policies.append(matches[0])
    return (store/relative).resolve(),files,carried,kept_records,superseded_out,policies

def publish_generation(batches,store,*,world,dimension,pipeline_key,expected_chunks,expected_revision=None,
                       min_free_bytes=1024**3,max_write_bytes=None,compress_chunks=False,reuse_unchanged=False,
                       lod_directory=None,area_record=None,carry_forward=None):
    """batches: [{geometry: directory, snapshotSha256: expected input SHA, coreChunks: [[cx,cz],...],
    optional stagedSource: {file: 'staged-source.json', sha256: expected sidecar SHA}}].

    lod_directory (optional) holds lod.json and lod/<x>_<z>-<sha>.json (UIE_LOD1, see lod_publication.py). It is
    validated against the published detail payloads and atlas; then manifest.atlasSha256 and manifest.lod are
    added and the coarse files stored like chunks. Without it, nothing changes.

    A supplied stagedSource sidecar is validated, stored byte-exact as provenance/<snapshot>-<sidecar>.json
    and listed in COMPLETE.sourcePolicies. Specs without it publish exactly as before; no coherence is claimed
    for them either way.

    expected_revision=None requires no previous publication. Otherwise it must exactly match the
    current generation, preventing an obsolete build from replacing a newer one. All batches of
    this generation must be supplied (not deltas). No data deletion is performed.

    area_record (optional, UIE_AREA_RECORD1 from area_records.build_area_record) is validated, bound tile by
    tile to the batches (snapshot, present chunks, capture manifest bounds/coverage/time via each batch's
    capture-manifest.json, atlas PNG and atlas-layout.json identity, lighting, sidecar hash and interval,
    aggregate) and written as COMPLETE.areas=[record]. Without it nothing changes and no auxiliary file is
    read. The record is checked for consistency with the published inputs, not for authenticity.

    carry_forward (optional, {'generation': expected_revision}; needs area_record) keeps every whole tile of
    the tracked base generation that the new area does not touch: its blobs are re-verified against the base
    inventory and their content hash and reused, its record is kept verbatim and its manifest values enter
    the aggregate. A base tile whose core equals a new tile's core is replaced (sourceInterval required on
    both, new start >= old end) and listed in COMPLETE.supersededTiles; partial overlap, another pipeline,
    atlas, layout or lighting, or an untracked base is refused. COMPLETE.areaBase names the base.
    """
    if not integer(min_free_bytes) or min_free_bytes<0:raise PublicationError('Invalid free disk reserve')
    if max_write_bytes is not None and (not integer(max_write_bytes) or max_write_bytes<0):
        raise PublicationError('Invalid publication write budget')
    if type(compress_chunks) is not bool:raise PublicationError('Invalid compression setting')
    if type(reuse_unchanged) is not bool:raise PublicationError('Invalid reuse setting')
    sha(pipeline_key)
    if expected_revision is not None:sha(expected_revision)
    if not all(isinstance(s,str) and s.strip() for s in (world,dimension)):raise PublicationError('Missing world identity')
    if not isinstance(batches,list) or not batches:raise PublicationError('No batches')
    def coords(values):
        if not isinstance(values,list) or not values:raise PublicationError('Expected chunk list required')
        result=set()
        for pair in values:
            if not isinstance(pair,(tuple,list)) or len(pair)!=2 or not all(integer(n) for n in pair):raise PublicationError('Invalid expected coordinates')
            key=tuple(pair)
            if key in result:raise PublicationError('Duplicate expected coordinates')
            result.add(key)
        return result
    required=coords(expected_chunks)
    area=None
    if area_record is not None:
        import area_records  # lazy: area_records imports helpers from this module
        try:area=area_records.validate_area_record(area_record)
        except area_records.AreaRecordError as error:raise PublicationError(f'Invalid area record: {error}') from None
        if area['pipelineKey']!=pipeline_key:raise PublicationError('Area record pipeline differs from the publication')
        area_tiles={t['snapshotSha256']:t for t in area['tiles']}
    if carry_forward is not None:
        if area is None:raise PublicationError('Carry-forward needs an area record')
        if not isinstance(carry_forward,dict) or set(carry_forward)!={'generation'}:raise PublicationError('Invalid carry-forward spec')
        if sha(carry_forward['generation'])!=expected_revision:raise PublicationError('Carry-forward base must be the expected revision')
    store=Path(store).resolve();store.mkdir(parents=True,exist_ok=True)
    roots=[Path(b['geometry']).resolve() for b in batches]
    lod_root=Path(lod_directory).resolve() if lod_directory is not None else None
    if any(store==r or store.is_relative_to(r) or r.is_relative_to(store) for r in roots+([lod_root] if lod_root else [])):
        raise PublicationError('Input and publication store overlap')
    lock=store/'.publish.lock'
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise PublicationError('Publisher lock exists; recover only after proving its writer stopped') from None
    os.close(fd)
    stage=None
    written=0
    def bounded_write(path,raw):
        nonlocal written
        if max_write_bytes is not None and written+len(raw)>max_write_bytes:
            raise PublicationError('Publication write budget exceeded')
        # Advisory guard before every write, not a filesystem reservation against other writers.
        if shutil.disk_usage(store).free<len(raw)+min_free_bytes:
            raise PublicationError('Free disk reserve would be exhausted')
        _write(path,raw);written+=len(raw)
    try:
        current_path=store/'current.json'
        current=json.loads(current_path.read_bytes()) if current_path.exists() else None
        if current is not None:
            if not isinstance(current,dict) or current.get('format')!='UIE_REVISION1':raise PublicationError('Invalid current pointer')
            generation=sha(current.get('generation'));manifest_sha=sha(current.get('manifestSha256'))
            if current.get('manifest')!=f'generations/{generation}/manifest.json':raise PublicationError('Invalid current manifest path')
            if digest(read_relative(store,current['manifest']))!=manifest_sha:raise PublicationError('Current generation is damaged')
        if (current.get('generation') if current else None)!=expected_revision:raise PublicationError('Stale publication base')
        identity={'world':world,'dimension':dimension,'pipelineKey':pipeline_key}
        if current and any(current.get(k)!=identity[k] for k in ('world','dimension')):
            raise PublicationError('Different world/dimension in store')
        previous_files={};previous_root=None;carry=None
        if carry_forward is not None:
            # The stale-base check above proved current == expected_revision == the carry-forward base.
            carry=_carry_plan(store,current['generation'],pipeline_key,area)
            previous_root,previous_files=carry[0],carry[1]
        elif current and reuse_unchanged:
            relative='generations/'+current['generation']
            inventory_raw=read_relative(store,relative+'/COMPLETE.json')
            if digest(inventory_raw)!=current['generation']:raise PublicationError('Previous inventory is damaged')
            previous=json.loads(inventory_raw)
            if previous.get('format')!='UIE_GENERATION1' or not isinstance(previous.get('files'),dict):
                raise PublicationError('Invalid previous inventory')
            previous_files=previous['files'];previous_root=(store/relative).resolve()
        stage=store/('.staging-'+uuid.uuid4().hex);stage.mkdir()
        files={};seen=set();chunks=[];blocks=[];inputs=[];atlas_sha=None;lighting=None
        mins=[math.inf]*3;maxs=[-math.inf]*3;names=set();counts={k:0 for k in ('renderedBlocks','unsupportedBlocks','emptyBlocks')}
        dates=[];sources=[];policies=[];detail_lod={}
        def put(name,raw):
            h=digest(raw)
            if name in files:
                if files[name]!=h:raise PublicationError('Output collision')
                return
            if previous_files.get(name)==h:
                if digest(read_relative(previous_root,name))!=h:raise PublicationError('Previous file is damaged')
                target=stage/name;target.parent.mkdir(parents=True,exist_ok=True)
                if shutil.disk_usage(store).free<min_free_bytes+4096:
                    raise PublicationError('Free disk reserve would be exhausted')
                try:os.link((previous_root/name).resolve(),target)
                except OSError as error:
                    if error.errno not in (errno.EXDEV,errno.EPERM,errno.EACCES,errno.ENOTSUP,errno.EOPNOTSUPP,errno.EMLINK):raise
                    bounded_write(target,raw)
            else:bounded_write(stage/name,raw)
            files[name]=h
        def put_chunk(name,raw):
            if not compress_chunks:return put(name,raw)
            compressed=gzip.compress(raw,compresslevel=6,mtime=0)
            # Python/zlib versions can emit a platform-specific OS byte with mtime=0.
            compressed=compressed[:9]+b'\xff'+compressed[10:]
            if gzip.decompress(compressed)!=raw:raise PublicationError('Compression roundtrip failed')
            put(name+'.gz',compressed)
        for spec,root in zip(batches,roots):
            def source(name):
                value=read_relative(root,name);sources.append(((root/name).resolve(),digest(value)));return value
            expected=sha(spec['snapshotSha256'])
            raw=source('manifest.json');m=json.loads(raw)
            if m.get('format')!=1 or m.get('snapshotFormat')!='UIE2' or m.get('snapshotSha256')!=expected:
                raise PublicationError('Batch snapshot provenance mismatch')
            ref=spec.get('stagedSource')
            if ref is not None:
                # Spec shape first, then the exact sidecar bytes via source() (re-hashed before publication).
                if not isinstance(ref,dict) or set(ref)!={'file','sha256'} or ref.get('file')!=STAGED_SOURCE_FILE:
                    raise PublicationError('Invalid stagedSource spec')
                sidecar=source(STAGED_SOURCE_FILE);record=staged_source_record(ref,sidecar,expected)
                put(record['file'],sidecar);policies.append(record)
            info=m.get('blockInfo',{})
            if info.get('format')!='UIE_BLOCKS1' or info.get('snapshotSha256')!=expected:raise PublicationError('Block provenance mismatch')
            light=m.get('bakedLighting',False)
            if type(light) is not bool or lighting is not None and light!=lighting:raise PublicationError('Mixed lighting modes')
            lighting=light
            atlas=source(m['atlas']);h=digest(atlas)
            if not atlas.startswith(b'\x89PNG\r\n\x1a\n'):raise PublicationError('Invalid atlas signature')
            if atlas_sha is not None and h!=atlas_sha:raise PublicationError('Mixed atlases cannot share one generation')
            atlas_sha=h;put('atlas.png',atlas)
            bindex={}
            for entry in info.get('chunks',[]):
                key=(entry.get('x'),entry.get('z'))
                if not all(integer(v) for v in key) or key in bindex:raise PublicationError('Duplicate/invalid block chunk')
                if entry['file']!=f'blocks/{expected}/chunk_{key[0]}_{key[1]}.json':raise PublicationError('Block file not bound to snapshot path')
                bindex[key]=entry['file']
            local=set()
            for entry in m.get('chunks',[]):
                key=(entry.get('x'),entry.get('z'))
                if not all(integer(v) for v in key) or key in seen:raise PublicationError('Duplicate/invalid geometry chunk')
                if key not in bindex:raise PublicationError('Missing block chunk')
                seen.add(key);local.add(key)
                payload=source(entry['file']);mesh=json.loads(payload);validate_mesh(mesh)
                if lod_root is not None:  # small per-chunk record only: payload hash and part counts
                    detail_lod[key]=(digest(payload),[(len(p['positions'])//3,len(p['indices'])) for p in [mesh,*mesh.get('transparentParts',[])]])
                del mesh
                bp=source(bindex[key]);validate_blocks(json.loads(bp),*key)
                mesh_name=f'chunks/{key[0]}_{key[1]}-{digest(payload)}.json'
                block_name=f'blocks/{key[0]}_{key[1]}-{digest(bp)}.json'
                put_chunk(mesh_name,payload);put_chunk(block_name,bp)
                chunks.append({'x':key[0],'z':key[1],'file':mesh_name});blocks.append({'x':key[0],'z':key[1],'file':block_name})
            if not local or local!=set(bindex):raise PublicationError('Core geometry/block chunk sets differ')
            if local!=coords(spec.get('coreChunks')):raise PublicationError('Expected core chunks differ')
            bounds=m.get('bounds',{})
            for k in ('min','max'):
                if not isinstance(bounds.get(k),list) or len(bounds[k])!=3 or not all(finite(v) for v in bounds[k]):raise PublicationError('Invalid bounds')
            if any(a>b for a,b in zip(bounds['min'],bounds['max'])):raise PublicationError('Reversed bounds')
            mins=[min(a,b) for a,b in zip(mins,bounds['min'])];maxs=[max(a,b) for a,b in zip(maxs,bounds['max'])]
            coverage=m.get('coverage',{})
            for k in counts:
                n=coverage.get(k,0)
                if not integer(n) or n<0:raise PublicationError('Invalid coverage')
                counts[k]+=n
            ns=coverage.get('unsupportedNames',[])
            if not isinstance(ns,list) or not all(isinstance(n,str) for n in ns):raise PublicationError('Invalid unsupported names')
            names.update(ns)
            from datetime import datetime
            try:dt=datetime.fromisoformat(m['generatedAt'].replace('Z','+00:00'))
            except (KeyError,ValueError,AttributeError):raise PublicationError('Invalid capture timestamp') from None
            if dt.tzinfo is None:raise PublicationError('Capture timestamp needs timezone')
            dates.append(dt);inputs.append(expected)
            if area is not None:
                tile=area_tiles.pop(expected,None)
                if tile is None:raise PublicationError('Area record has no unused tile for this batch snapshot')
                atlas_bound,layout_identity=_area_tile(tile,root,source,m,local,ref,sidecar if ref is not None else None)
                if atlas_bound!=h or h!=area['atlasSha256'] or layout_identity!=area['layoutIdentitySha256'] \
                        or light is not area['bakedLighting']:
                    raise PublicationError('Area record atlas, layout identity or lighting differs from the batch')
        if area is not None:
            if area_tiles:raise PublicationError('Area record has tiles without a batch')
            aggregate=area['aggregate']
            if aggregate['chunks']!=len(seen) or canonical(aggregate['bounds'])!=canonical({'min':mins,'max':maxs}) \
                    or canonical(aggregate['coverage'])!=canonical({**counts,'unsupportedNames':sorted(names)}):
                raise PublicationError('Area record aggregate differs from the published batches')
        if carry is not None:
            base_root,base_files,carried,kept_records,superseded_out,carried_policies=carry
            def base_source(name):
                value=read_relative(base_root,name)
                if digest(value)!=base_files.get(name):raise PublicationError('Carried base file is damaged')
                sources.append(((base_root/name).resolve(),digest(value)));return value
            def base_payload(name):
                match=re.fullmatch(r'(chunks|blocks)/(-?[0-9]+)_(-?[0-9]+)-([0-9a-f]{64})\.json',name)
                if not match:raise PublicationError('Invalid carried file name')
                if name in base_files:payload=base_source(name)
                elif name+'.gz' in base_files:payload=gzip.decompress(base_source(name+'.gz'))
                else:raise PublicationError('Carried file missing from the base inventory')
                if digest(payload)!=match.group(4):raise PublicationError('Carried file content differs from its name')
                return payload
            base_manifest=json.loads(base_source('manifest.json'))
            mesh_names={(e['x'],e['z']):e['file'] for e in base_manifest['chunks']}
            block_names={(e['x'],e['z']):e['file'] for e in base_manifest['blockInfo']['chunks']}
            for _,tile in carried:
                for key in map(tuple,tile['present']):
                    if key in seen:raise PublicationError('Carried chunk collides with the new area')
                    seen.add(key)
                    mesh_name,block_name=mesh_names[key],block_names[key]
                    if not isinstance(mesh_name,str) or not isinstance(block_name,str) \
                            or not mesh_name.startswith(f'chunks/{key[0]}_{key[1]}-') or not block_name.startswith(f'blocks/{key[0]}_{key[1]}-'):
                        raise PublicationError('Carried file not bound to its chunk')
                    payload=base_payload(mesh_name);mesh=json.loads(payload);validate_mesh(mesh)
                    if lod_root is not None:
                        detail_lod[key]=(digest(payload),[(len(p['positions'])//3,len(p['indices'])) for p in [mesh,*mesh.get('transparentParts',[])]])
                    del mesh
                    bp=base_payload(block_name);validate_blocks(json.loads(bp),*key)
                    put_chunk(mesh_name,payload);put_chunk(block_name,bp)
                    chunks.append({'x':key[0],'z':key[1],'file':mesh_name});blocks.append({'x':key[0],'z':key[1],'file':block_name})
                bounds=tile['bounds']
                mins=[min(a,b) for a,b in zip(mins,bounds['min'])];maxs=[max(a,b) for a,b in zip(maxs,bounds['max'])]
                for k in counts:counts[k]+=tile['coverage'][k]
                names.update(tile['coverage']['unsupportedNames'])
                dates.append(_utc(tile['generatedAt']));inputs.append(tile['snapshotSha256'])
            for policy in carried_policies:
                put(policy['file'],base_source(policy['file']));policies.append(policy)
        lod_block=None
        if lod_root is not None:
            import lod_publication  # only when requested: no-LOD publication is unchanged
            def lod_source(name):
                value=read_relative(lod_root,name);sources.append(((lod_root/name).resolve(),digest(value)));return value
            lod_block=lod_publication.publish_lod(lod_source,detail_lod,atlas_sha,put_chunk)
        for path,h in sources:
            if digest(path.read_bytes())!=h:raise PublicationError('Source file changed during assembly')
        if seen!=required:raise PublicationError('Generation does not contain all expected chunks')
        order=lambda c:(c['z'],c['x'])
        manifest={'format':1,'title':'UIE · dávkový export','sample':True,'generatedAt':max(dates).isoformat(),
                  'atlas':'atlas.png','bakedLighting':lighting,'bounds':{'min':mins,'max':maxs},
                  'coverage':{**counts,'unsupportedNames':sorted(names)},'chunks':sorted(chunks,key=order),
                  'blockInfo':{'format':'UIE_BLOCKS1','chunks':sorted(blocks,key=order)}}
        if lod_block is not None:manifest['atlasSha256']=atlas_sha;manifest['lod']=lod_block
        put('manifest.json',canonical(manifest))
        inventory={'format':'UIE_GENERATION1',**identity,'pipelineIdentitySource':'caller-assertion',
                   'snapshots':sorted(inputs),'files':files}
        # Only when supplied, so plain generations keep their exact inventory and generation id.
        if policies:inventory['sourcePolicies']=sorted(policies,key=lambda p:(p['snapshotSha256'],p['sha256']))
        if area is not None:inventory['areas']=[area]  # opt-in: untracked generations are byte-identical
        if carry is not None:
            inventory['areas']=kept_records+[area];inventory['supersededTiles']=superseded_out
            inventory['areaBase']=current['generation']
        inventory_bytes=canonical(inventory);generation=digest(inventory_bytes)
        bounded_write(stage/'COMPLETE.json',inventory_bytes)
        # Independently reread every staged file before the pointer can refer to it.
        for name,h in files.items():
            if digest((stage/name).read_bytes())!=h:raise PublicationError('Staged output changed')
        for directory in sorted((p for p in stage.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):
            _sync_dir(directory)
        _sync_dir(stage)
        pointer={'format':'UIE_REVISION1',**identity,'generation':generation,
                 'manifest':f'generations/{generation}/manifest.json','manifestSha256':files['manifest.json']}
        pointer_raw=canonical(pointer)
        # Predictable pointer failures abort here, while only the private stage exists.
        # written already includes COMPLETE.json; only the pointer bytes are still to come.
        if max_write_bytes is not None and written+len(pointer_raw)>max_write_bytes:
            raise PublicationError('Publication write budget exceeded')
        if shutil.disk_usage(store).free<len(pointer_raw)+min_free_bytes:
            raise PublicationError('Free disk reserve would be exhausted')
        parent=store/'generations'
        if parent.resolve()!=parent:raise PublicationError('Generation directory redirected')
        parent.mkdir(exist_ok=True);target=parent/generation
        if target.is_symlink():raise PublicationError('Generation target redirected')
        if target.exists():
            if (target/'COMPLETE.json').read_bytes()!=inventory_bytes or any(digest((target/n).read_bytes())!=h for n,h in files.items()):
                raise PublicationError('Existing immutable generation is damaged')
        else:os.rename(stage,target)
        _sync_dir(parent)
        temp=store/('.current-'+uuid.uuid4().hex+'.tmp')
        try:bounded_write(temp,pointer_raw)
        except FileExistsError:raise  # never remove a temp file this invocation did not create
        except BaseException:
            temp.unlink(missing_ok=True);raise
        # The exclusive write succeeded, so this temp is ours even if replace raises EEXIST.
        try:os.replace(temp,current_path)
        finally:temp.unlink(missing_ok=True)
        _sync_dir(store)
        return pointer
    finally:
        try:
            if stage is not None and stage.exists():
                # Only this invocation's freshly-created, checked staging directory is disposable.
                if stage.resolve().parent!=store or stage.is_symlink() or not stage.name.startswith('.staging-'):
                    raise PublicationError('Refusing cleanup of redirected staging directory')
                shutil.rmtree(stage)
        finally:lock.unlink(missing_ok=True)

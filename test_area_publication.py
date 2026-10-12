"""Tests for optional area records in publish_generation (task93).

- Golden compatibility: untracked calls of the modified publisher are compared byte-for-byte with the publisher
  as it was before this change (BASELINE_SOURCE: every statement unchanged, comments trimmed; run as its own module).
- Tracked publication: a real prepared plan, synthetic GeometryCapture-shaped results, the real verifier, a real
  area record (area_records.build_area_record) and the candidate adapter's real staging (publish_attempt._stage).
No queue, no Minecraft, no LIVE. Stdlib only; runs on Windows and Linux."""
import copy
import hashlib
import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path

import area_records
import capture_batch_file as cbf
import publish_attempt as pa
from pipeline_identity import create_pipeline_identity
from prepare_warm_plan import prepare_warm_plan
from publish_generation import PublicationError, publish_generation
from test_area_records import JOB, envelope, render
from test_prepare_warm_plan import build_world
from test_publish_attempt import make_staged

sha = lambda raw: hashlib.sha256(raw).hexdigest()

# publish_generation.py as it was before task93: every statement unchanged; only comments and the long function
# docstring were trimmed. The untracked behaviour of the current publisher must be byte-identical to it.
BASELINE_SOURCE = r'''"""Offline immutable generation assembly with one atomic revision pointer.

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
    if resolved.stat().st_size>128*1024*1024:raise PublicationError('Oversized batch file')
    with resolved.open('rb') as stream:raw=stream.read(128*1024*1024+1)
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

def publish_generation(batches,store,*,world,dimension,pipeline_key,expected_chunks,expected_revision=None,
                       min_free_bytes=1024**3,max_write_bytes=None,compress_chunks=False,reuse_unchanged=False,
                       lod_directory=None):
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
        previous_files={};previous_root=None
        if current and reuse_unchanged:
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
                if lod_root is not None:
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
        lod_block=None
        if lod_root is not None:
            import lod_publication
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
        if policies:inventory['sourcePolicies']=sorted(policies,key=lambda p:(p['snapshotSha256'],p['sha256']))
        inventory_bytes=canonical(inventory);generation=digest(inventory_bytes)
        bounded_write(stage/'COMPLETE.json',inventory_bytes)
        for name,h in files.items():
            if digest((stage/name).read_bytes())!=h:raise PublicationError('Staged output changed')
        for directory in sorted((p for p in stage.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):
            _sync_dir(directory)
        _sync_dir(stage)
        pointer={'format':'UIE_REVISION1',**identity,'generation':generation,
                 'manifest':f'generations/{generation}/manifest.json','manifestSha256':files['manifest.json']}
        pointer_raw=canonical(pointer)
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
        except FileExistsError:raise
        except BaseException:
            temp.unlink(missing_ok=True);raise
        try:os.replace(temp,current_path)
        finally:temp.unlink(missing_ok=True)
        _sync_dir(store)
        return pointer
    finally:
        try:
            if stage is not None and stage.exists():
                if stage.resolve().parent!=store or stage.is_symlink() or not stage.name.startswith('.staging-'):
                    raise PublicationError('Refusing cleanup of redirected staging directory')
                shutil.rmtree(stage)
        finally:lock.unlink(missing_ok=True)
'''


def baseline_module():
    module = types.ModuleType("publish_generation_baseline")
    exec(compile(BASELINE_SOURCE, "publish_generation_baseline.py", "exec"), module.__dict__)
    return module


def tree(root):
    return {str(p.relative_to(root)).replace("\\", "/"): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class AreaPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        base = Path(cls._temp.name).resolve()
        level, region = build_world(base / "world")
        batches = base / "batches"
        batches.mkdir()
        for name, tile in (("A", (-1, -1)), ("B", (0, -1))):
            cbf.create_batch(level, region, *cbf.tile_areas(*tile), batches / name)
            shutil.copytree(batches / name, batches / f"{name}-staged")
            make_staged(batches / f"{name}-staged")
        (base / "exporter.jar").write_bytes(b"exporter")
        identity = create_pipeline_identity({}, {"exporter": base / "exporter.jar"}, {"java": "8"})
        cls.key = identity["pipelineKey"]
        cls.templates = {}
        for kind, names in (("plain", ("A", "B")), ("staged", ("A-staged", "B-staged"))):
            cls.templates[kind] = base / f"template-{kind}"
            prepare_warm_plan([str(batches / n) for n in names], cls.templates[kind], world="uie", dimension="0",
                              pipeline_key=cls.key, min_free_bytes=0, pipeline_identity=identity)
        cls.baseline = baseline_module()

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name).resolve()
        self.stores = 0

    def prepared(self, kind="plain"):
        """(staged specs/outputs, area record) exactly as the candidate adapter produces them."""
        plan = self.dir / f"plan-{kind}"
        shutil.copytree(self.templates[kind], plan)
        shutil.copyfile(plan / "COMPLETE", plan / "PLAN-COMPLETE.json")
        render(plan)
        plan_sha = sha((plan / "plan.json").read_bytes())
        raw = envelope(plan, plan_sha, self.key)
        record = area_records.build_area_record(plan, raw, verified_sha256=sha(raw), job_id=JOB, attempt=1)
        stage = self.dir / f"stage-{kind}"
        stage.mkdir()
        staged = pa._stage(plan, stage, pa._plan_jobs(plan, json.loads(raw)["report"]))
        return staged, record

    def store(self):
        self.stores += 1
        return self.dir / f"store-{self.stores}"

    def publish(self, staged, store, publisher=publish_generation, **kwargs):
        kwargs.setdefault("compress_chunks", True)
        return publisher(staged["specs"], store, world="uie", dimension="0", pipeline_key=self.key,
                         expected_chunks=staged["expected"], min_free_bytes=0, **kwargs)

    # ------------------------------------------------------------------ golden compatibility

    def test_untracked_calls_are_byte_identical_to_the_baseline_publisher(self):
        for kind in ("plain", "staged"):
            staged, _ = self.prepared(kind)
            for compress in (True, False):
                with self.subTest(kind=kind, compress=compress):
                    old, new = self.store(), self.store()
                    expected = self.publish(staged, old, publisher=self.baseline.publish_generation,
                                            compress_chunks=compress)
                    pointer = self.publish(staged, new, compress_chunks=compress)
                    self.assertEqual(pointer, expected, "same generation id and pointer")
                    self.assertEqual(tree(new), tree(old), "every published byte is identical")
                    inventory = json.loads((new / "generations" / pointer["generation"] / "COMPLETE.json").read_bytes())
                    self.assertNotIn("areas", inventory)

    def test_untracked_calls_need_no_auxiliary_files(self):
        staged, _ = self.prepared()
        for spec in staged["specs"]:
            for name in ("capture-manifest.json", "atlas-layout.json"):
                (Path(spec["geometry"]) / name).unlink()
        self.assertIn("generation", self.publish(staged, self.store()))

    # ------------------------------------------------------------------ tracked publication

    def test_tracked_candidate_records_the_area_and_keeps_every_payload(self):
        for kind in ("plain", "staged"):
            with self.subTest(kind):
                staged, record = self.prepared(kind)
                plain, tracked = self.store(), self.store()
                untracked_pointer = self.publish(staged, plain)
                pointer = self.publish(staged, tracked, area_record=record)
                self.assertNotEqual(pointer["generation"], untracked_pointer["generation"])
                self.assertEqual(pointer["manifestSha256"], untracked_pointer["manifestSha256"], "same manifest")
                inventory = json.loads((tracked / "generations" / pointer["generation"] / "COMPLETE.json").read_bytes())
                old = json.loads((plain / "generations" / untracked_pointer["generation"] / "COMPLETE.json").read_bytes())
                self.assertEqual(inventory.pop("areas"), [record], "exactly the validated record")
                self.assertEqual(inventory, old, "everything else is the untracked inventory")
                self.assertEqual({k: v for k, v in tree(tracked).items() if "COMPLETE.json" not in k and k != "current.json"},
                                 {k.replace(untracked_pointer["generation"], pointer["generation"]): v
                                  for k, v in tree(plain).items() if "COMPLETE.json" not in k and k != "current.json"})

    def assert_refused(self, staged, record, pattern=""):
        store = self.store()
        with self.assertRaisesRegex(PublicationError, pattern):
            self.publish(staged, store, area_record=record)
        self.assertFalse((store / "current.json").exists(), "no pointer")
        self.assertFalse((store / "generations").exists(), "no generation")
        self.assertFalse((store / ".publish.lock").exists(), "lock released")

    def changed(self, record, change, reaggregate=True):
        doc = copy.deepcopy(record)
        change(doc)
        if reaggregate:   # keep the record internally consistent so the publisher's binding is what refuses it
            doc["aggregate"] = area_records._aggregate(doc["tiles"])
        return doc

    def test_tampered_records_are_refused_before_the_pointer(self):
        staged, record = self.prepared()
        first = lambda r: r["tiles"][0]

        def move_present(r):
            first(r)["absent"].append(first(r)["present"].pop())
            first(r)["absent"].sort(key=lambda c: (c[1], c[0]))
        cases = {
            "pipeline": (lambda r: r.update(pipelineKey="3" * 64), "pipeline"),
            "atlas": (lambda r: r.update(atlasSha256="4" * 64), "atlas"),
            "layout identity": (lambda r: r.update(layoutIdentitySha256="5" * 64), "layout"),
            "lighting": (lambda r: r.update(bakedLighting=False), "lighting"),
            "tile coverage": (lambda r: first(r)["coverage"].update(renderedBlocks=first(r)["coverage"]["renderedBlocks"] + 1),
                              "capture manifest"),
            "tile capture time": (lambda r: first(r).update(generatedAt="2026-10-02T00:00:00Z"), "capture manifest"),
            "tile bounds": (lambda r: first(r)["bounds"]["max"].__setitem__(1, 255), "capture manifest"),
            "tile manifest hash": (lambda r: first(r).update(manifestSha256="6" * 64), "Capture manifest"),
            "present chunks": (move_present, "present"),
            "claimed sidecar": (lambda r: first(r).update(stagedSourceSha256="7" * 64), "staged source"),
            "missing tile": (lambda r: r["tiles"].pop(), "no unused tile"),
            "foreign snapshot": (lambda r: first(r).update(snapshotSha256="8" * 64), "no unused tile"),
        }
        for label, (change, pattern) in cases.items():
            with self.subTest(label):
                self.assert_refused(staged, self.changed(record, change), pattern)
        with self.subTest("inconsistent aggregate"):
            self.assert_refused(staged, self.changed(record, lambda r: r["aggregate"].update(chunks=1), False),
                                "Invalid area record")

    def test_tampered_auxiliary_and_working_inputs_are_refused_before_the_pointer(self):
        def edit(name, change):
            def apply(staged):
                path = Path(staged["specs"][0]["geometry"]) / name
                change(path)
            return apply
        cases = {
            "capture manifest bytes": (edit("capture-manifest.json", lambda p: p.write_bytes(p.read_bytes() + b" ")),
                                       "Capture manifest"),
            "capture manifest missing": (edit("capture-manifest.json", lambda p: p.unlink()), "needs capture-manifest"),
            "layout bytes": (edit("atlas-layout.json", lambda p: p.write_bytes(p.read_bytes() + b" ")), "layout"),
            "layout missing": (edit("atlas-layout.json", lambda p: p.unlink()), "needs atlas-layout"),
            "working manifest beyond blockInfo": (
                edit("manifest.json", lambda p: p.write_text(json.dumps({**json.loads(p.read_text()), "note": "x"}))),
                "beyond the added blockInfo"),
        }
        for label, (change, pattern) in cases.items():
            with self.subTest(label):
                self.setUp()
                staged, record = self.prepared()
                change(staged)
                self.assert_refused(staged, record, pattern)

    def test_staged_source_binding_and_the_existing_interval_policy(self):
        staged, record = self.prepared("staged")
        first = lambda r: r["tiles"][0]
        cases = {
            "other sidecar hash": lambda r: first(r).update(stagedSourceSha256="9" * 64),
            "no sidecar claimed": lambda r: first(r).update(stagedSourceSha256=None, sourceInterval=None),
            "unknown interval": lambda r: first(r).update(sourceInterval=None),
            "other interval": lambda r: first(r)["sourceInterval"].update(startUtc="2026-09-30T09:00:00Z"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.assert_refused(staged, self.changed(record, change), "staged source")
        self.assertIn("generation", self.publish(staged, self.store(), area_record=record))


if __name__ == "__main__":
    unittest.main()

"""Explicit cube catalog for Mesher; separate from the legacy exported catalog.

No metadata inheritance or renderType/class heuristics. Supported by world_hires.
"""
import argparse,hashlib,json,math,os,shutil,tempfile
from pathlib import Path
import atlas_identity
import catalog_package
import png_io
from offline_catalog import Model,FALLBACK_TINT

FORMAT='UIEMAP_EXPLICIT_CATALOG1'


def sha(raw):return hashlib.sha256(raw).hexdigest()
def canonical(doc):return json.dumps(doc,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def read_json(path):
    return json.loads(atlas_identity._read(path,16*1024*1024,'catalog JSON'),
                      object_pairs_hook=atlas_identity._no_duplicates,parse_constant=atlas_identity._reject_constant)
def hexsha(value):return isinstance(value,str) and atlas_identity.HEX64.fullmatch(value)
def uv(value):
    if not isinstance(value,(list,tuple)) or len(value)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in value) or value[0]>=value[2] or value[1]>=value[3]:
        raise ValueError('Invalid explicit UV')


def prepare(source):
    source=Path(source).absolute()
    if source.resolve()!=source:raise ValueError('Linked atlas directory refused')
    bindings=read_json(source/'bindings.json');layout=read_json(source/'atlas.json')
    if bindings.get('format')!='UIEMAP_MODPACK_BINDINGS1' or bindings.get('unlistedMetadataPolicy')!='fallback':raise ValueError('Invalid bindings format/policy')
    identity={k:bindings[k] for k in ('registrySha256','textures','variants')}
    if not hexsha(bindings['registrySha256']) or sha(canonical(identity))!=bindings.get('bindingSha256'):raise ValueError('Bindings identity mismatch')
    if layout.get('format')!='UIEMAP_RULE_ATLAS1' or layout.get('bindingSha256')!=bindings['bindingSha256']:raise ValueError('Atlas/bindings mismatch')
    raw=atlas_identity._read(source/'atlas.png',256*1024*1024,'atlas PNG')
    if sha(raw)!=layout.get('atlasSha256'):raise ValueError('Atlas PNG identity mismatch')
    catalog_package.verify_png(raw);width,height,channels,pixels=png_io.decode(raw)
    if channels!=4 or width!=layout.get('width') or height!=layout.get('height') or width<4 or max(width,height)>4096:raise ValueError('Invalid RGBA atlas dimensions')
    sprites=layout.get('sprites');refs=bindings['textures']
    if not isinstance(sprites,dict) or not isinstance(refs,dict) or not 1<=len(sprites)<=256 or set(sprites)!=set(refs):raise ValueError('Invalid atlas sprites')
    new_height=height+4;faces={};transparent={}
    for alias,s in sprites.items():
        x,y,w,h=[s[k] for k in ('x','y','width','height')]
        if any(type(v) is not int for v in (x,y,w,h)) or min(x,y)<1 or min(w,h)<1 or x+w>=width or y+h>=height:raise ValueError('Sprite outside atlas/gutter')
        if s.get('uv')!=[x/width,y/height,(x+w)/width,(y+h)/height]:raise ValueError('Sprite UV differs from pixels')
        if (w,h)!=(refs[alias]['width'],refs[alias]['height']) or s.get('sourceSha256')!=refs[alias]['sha256']:raise ValueError('Sprite source differs from bindings')
        extracted=b''.join(pixels[((y+dy)*width+x)*4:((y+dy)*width+x+w)*4] for dy in range(h))
        if sha(extracted)!=s.get('rgbaSha256'):raise ValueError('Sprite pixels mismatch')
        transparent[alias]=any(v!=255 for v in extracted[3::4])
        faces[alias]=[x/width,y/new_height,(x+w)/width,(y+h)/new_height]
    variants=bindings['variants']
    if not isinstance(variants,list) or not 1<=len(variants)<=65536:raise ValueError('Invalid variants')
    models=[];seen=set()
    for row in variants:
        name=row['name'];meta=row['metadata'];aliases=row['faces'];evidence=row['evidence']
        if not isinstance(name,str) or ':' not in name or name=='minecraft:air' or type(meta) is not int or not 0<=meta<=15:raise ValueError('Invalid explicit block variant')
        if (name,meta) in seen:raise ValueError('Duplicate explicit variant')
        seen.add((name,meta))
        if row.get('model')!='cube' or row.get('opacity') not in ('solid','cutout') or row.get('tier')!='user-declared':raise ValueError('Unsupported explicit model')
        if not isinstance(aliases,list) or len(aliases)!=6 or any(not isinstance(a,str) or a not in faces for a in aliases):raise ValueError('Invalid face aliases')
        if not isinstance(evidence,str) or not 1<=len(evidence)<=2048:raise ValueError('Invalid evidence')
        if row['opacity']=='solid' and any(transparent[a] for a in aliases):raise ValueError('Solid model contains transparency')
        models.append(dict(name=name,metadata=meta,opacity=row['opacity'],faces=[faces[a] for a in aliases],evidence=evidence))
    # Dedicated opaque checker, with gutter, independent of user cutout textures.
    out=bytearray(width*new_height*4);out[:len(pixels)]=pixels
    for dy in range(4):
        for dx in range(4):
            value=255 if min(max(dx-1,0),1)==min(max(dy-1,0),1) else 32
            pos=((height+dy)*width+dx)*4;out[pos:pos+4]=bytes([value,value,value,255])
    missing=[1/width,(height+1)/new_height,3/width,(height+3)/new_height]
    image=png_io.encode(width,new_height,4,out)
    doc=dict(format=FORMAT,registrySha256=bindings['registrySha256'],bindingSha256=bindings['bindingSha256'],
             atlasSha256=sha(image),missing=missing,models=sorted(models,key=lambda m:(m['name'],m['metadata'])),
             unlistedMetadataPolicy='fallback',modelSemanticsVerified=False)
    doc['identity']=sha(canonical(doc))
    return image,doc


class ExplicitCatalog:
    def __init__(self,doc):
        self.document=doc
        self.identity=doc['identity'];self.registry_sha256=doc['registrySha256'];self.missing_uv=tuple(doc['missing'])
        self.sprites={};self._models={}
        for row in doc['models']:
            self._models[(row['name'],row['metadata'])]=Model('cube','assumed',row['opacity'],[tuple(v) for v in row['faces']],
                reason='User-declared cube; semantics not verified: '+row['evidence'])
    def model(self,name,meta):
        if name=='minecraft:air':return Model('none','invisible',reason='air')
        return self._models.get((name,meta)) or Model('fallback','fallback',faces=[self.missing_uv]*6,tint='const',color=FALLBACK_TINT,reason='No explicit rule for this exact metadata')
    @classmethod
    def load(cls,source,registry):
        from modpack_registry import compare
        root=Path(source).absolute()
        if root.resolve()!=root:raise ValueError('Linked catalog directory refused')
        doc=read_json(root/'model-catalog.json');identity=doc.pop('identity',None)
        if doc.get('format')!=FORMAT or sha(canonical(doc))!=identity:raise ValueError('Explicit catalog identity mismatch')
        if doc.get('registrySha256')!=compare(registry,[],{})['registrySha256']:raise ValueError('Catalog belongs to a different world registry')
        if doc.get('unlistedMetadataPolicy')!='fallback' or doc.get('modelSemanticsVerified') is not False:raise ValueError('Invalid explicit policy')
        uv(doc.get('missing'));rows=doc.get('models');seen=set()
        if not isinstance(rows,list) or not 1<=len(rows)<=65536:raise ValueError('Invalid model count')
        names=set(registry.values())
        for row in rows:
            name=row.get('name');meta=row.get('metadata')
            if not isinstance(name,str) or name not in names or name=='minecraft:air' or type(meta) is not int or not 0<=meta<=15 or (name,meta) in seen:raise ValueError('Invalid or duplicate model key')
            seen.add((name,meta))
            if row.get('opacity') not in ('solid','cutout') or not isinstance(row.get('evidence'),str):raise ValueError('Invalid model properties')
            if not isinstance(row.get('faces'),list) or len(row['faces'])!=6:raise ValueError('Invalid model faces')
            for face in row['faces']:uv(face)
        raw=atlas_identity._read(root/'atlas.png',256*1024*1024,'catalog atlas')
        if sha(raw)!=doc.get('atlasSha256'):raise ValueError('Catalog atlas mismatch')
        catalog_package.verify_png(raw)
        width,height,channels,pixels=png_io.decode(raw)
        if channels!=4 or width>4096 or height>4100:raise ValueError('Invalid catalog RGBA dimensions')
        def opaque_rectangle(face):
            coords=[face[0]*width,face[1]*height,face[2]*width,face[3]*height]
            if any(abs(v-round(v))>1e-7 for v in coords):raise ValueError('UV is not pixel aligned')
            x0,y0,x1,y1=map(round,coords)
            return all(pixels[(y*width+x)*4+3]==255 for y in range(y0,y1) for x in range(x0,x1))
        if not opaque_rectangle(doc['missing']):raise ValueError('Fallback must be opaque')
        for row in rows:
            for face in row['faces']:
                opaque=opaque_rectangle(face)
                if row['opacity']=='solid' and not opaque:raise ValueError('Solid model contains transparency')
        doc['identity']=identity
        return cls(doc)

    def atlas_assets(self,source):
        """Snapshot verified PNG and a viewer-compatible layout for these exact models."""
        raw=atlas_identity._read(Path(source)/'atlas.png',256*1024*1024,'catalog atlas')
        if sha(raw)!=self.document['atlasSha256']:raise ValueError('Catalog atlas changed before rendering')
        width,height,_,_=png_io.decode(raw)
        def sprite(name,face):
            return dict(iconName=name,state='placed',x=round(face[0]*width),y=round(face[1]*height),
                        width=round((face[2]-face[0])*width),height=round((face[3]-face[1])*height),
                        rotated=False,uv=list(map(float,face)),getterUv=list(map(float,face)),getterUvMatches=True)
        missing=sprite('uiemap:missing',self.missing_uv)
        uploaded={'uiemap:missing':missing}
        for i,face in enumerate(sorted({tuple(f) for r in self.document['models'] for f in r['faces']})):
            name='uiemap:explicit/'+str(i);uploaded[name]=sprite(name,face)
        layout=dict(format='UIE_ATLAS_LAYOUT1',width=width,height=height,reusable=True,reuseBlockers=[],
                    missing=missing,registered=uploaded,uploaded=uploaded,atlasSha256=sha(raw))
        return {'atlas.png':raw,'atlas-layout.json':canonical(layout)}


def build(source,out,apply=False):
    source=Path(source).absolute();dest=Path(out).absolute()
    if dest.resolve()!=dest or dest.is_relative_to(source.resolve()):raise ValueError('Output must be separate and unlinked')
    if dest.exists() or not dest.parent.is_dir():raise ValueError('Output must be new with an existing parent')
    raw,doc=prepare(source)
    result=dict(format='UIEMAP_EXPLICIT_CATALOG_REPORT1',models=len(doc['models']),identity=doc['identity'],
                mesherReady=True,batchRendererReady=True,modelSemanticsVerified=False,readOnly=not apply)
    if not apply:return result
    lock=dest.with_name(dest.name+'.catalog-lock');stage=None
    with lock.open('x'):pass
    try:
        if dest.exists():raise ValueError('Output appeared during build')
        stage=Path(tempfile.mkdtemp(prefix='.explicit-catalog-',dir=dest.parent))
        (stage/'atlas.png').write_bytes(raw);(stage/'model-catalog.json').write_bytes(canonical(doc))
        (stage/'report.json').write_bytes(canonical(result))
        for path in stage.iterdir():
            with path.open('r+b') as stream:os.fsync(stream.fileno())
        if dest.exists():raise ValueError('Output appeared during build')
        stage.rename(dest);stage=None
    finally:
        if stage is not None:
            if stage.resolve().parent!=dest.parent.resolve() or not stage.name.startswith('.explicit-catalog-'):raise ValueError('Invalid staging cleanup')
            shutil.rmtree(stage)
        lock.unlink()
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True,type=Path);p.add_argument('--out',required=True,type=Path);p.add_argument('--apply',action='store_true');args=p.parse_args(argv)
    try:result=build(args.source,args.out,args.apply)
    except (OSError,ValueError,KeyError,TypeError) as error:p.exit(2,str(error)+'\n')
    print(json.dumps(result,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())

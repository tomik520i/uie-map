"""Build an isolated texture atlas from explicit rules. Not yet a renderer catalog."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile
import modpack_rules
import modpack_textures
import texture_pixels
import png_io

MAX_TEXTURES=256
MAX_ATLAS_SIDE=4096
MAX_PIXELS=4*1024*1024


def source_pixels(root,ref):
    path=root/ref['archive']
    if path.is_symlink():raise ValueError('Linked texture archive refused')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):raise ValueError('Texture archive is not regular')
        with zipfile.ZipFile(stream) as archive:
            entries=[i for i in archive.infolist() if i.filename==ref['entry']]
            if len(entries)!=1:raise ValueError('Selected texture absent or duplicated')
            if ref['entry']+'.mcmeta' in archive.namelist():raise ValueError('Animation metadata appeared during build')
            info=entries[0]
            if info.flag_bits&1 or stat.S_ISLNK(info.external_attr>>16) or not 1<=info.file_size<=modpack_textures.MAX_TEXTURE_BYTES:
                raise ValueError('Invalid selected texture entry')
            with archive.open(info) as source:raw=source.read(info.file_size+1)
        after=os.fstat(stream.fileno());named=path.lstat()
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        if identity(before)!=identity(after) or identity(after)!=identity(named) or stat.S_ISLNK(named.st_mode):
            raise ValueError('Texture archive changed during atlas build')
    if hashlib.sha256(raw).hexdigest()!=ref['sha256']:raise ValueError('Selected PNG changed after rule validation')
    width,height,pixels=texture_pixels.decode(raw)
    if width!=ref['width'] or height!=ref['height']:raise ValueError('Texture dimensions changed')
    return width,height,pixels


def prepare(mods,world,rules):
    bindings=modpack_rules.inspect(mods,world,rules);refs=bindings['textures']
    if len(refs)>MAX_TEXTURES:raise ValueError('Atlas supports at most 256 selected textures')
    if sum(r['width']*r['height'] for r in refs.values())>MAX_PIXELS:raise ValueError('Selected texture pixels exceed atlas budget')
    cell=max(r['width'] for r in refs.values())+2
    cols=math.isqrt(len(refs)-1)+1;rows=(len(refs)+cols-1)//cols
    power=lambda v:1<<(v-1).bit_length()
    width,height=power(cols*cell),power(rows*cell)
    if max(width,height)>MAX_ATLAS_SIDE:raise ValueError('Atlas exceeds 4096 side limit; split the rules')
    pixels=bytearray(width*height*4);sprites={}
    for n,(alias,ref) in enumerate(sorted(refs.items())):
        w,h,rgba=source_pixels(Path(mods).absolute(),ref);x=n%cols*cell+1;y=n//cols*cell+1
        # One-pixel extruded gutter prevents filtering from sampling another tile.
        for dy in range(-1,h+1):
            sy=max(0,min(h-1,dy));row=rgba[sy*w*4:(sy+1)*w*4]
            expanded=row[:4]+row+row[-4:];start=((y+dy)*width+x-1)*4
            pixels[start:start+len(expanded)]=expanded
        sprites[alias]=dict(x=x,y=y,width=w,height=h,uv=[x/width,y/height,(x+w)/width,(y+h)/height],
                            sourceSha256=ref['sha256'],rgbaSha256=hashlib.sha256(rgba).hexdigest(),
                            hasTransparency=any(a!=255 for a in rgba[3::4]))
    for variant in bindings['variants']:
        if variant['opacity']=='solid' and any(sprites[f]['hasTransparency'] for f in variant['faces']):
            raise ValueError('Solid rule references transparent pixels: '+variant['name'])
    raw=png_io.encode(width,height,4,pixels)
    layout=dict(format='UIEMAP_RULE_ATLAS1',width=width,height=height,gutter=1,sprites=sprites,
                atlasSha256=hashlib.sha256(raw).hexdigest(),bindingSha256=bindings['bindingSha256'])
    report=dict(format='UIEMAP_RULE_ATLAS_REPORT1',textures=len(refs),variants=len(bindings['variants']),
                width=width,height=height,atlasBytes=len(raw),atlasSha256=layout['atlasSha256'],
                bindingSha256=bindings['bindingSha256'],catalogReady=False,modelSemanticsVerified=False,
                limitations=['Texture atlas only: no renderer model catalog or production publication.',
                             'Encoded color samples preserved; gamma/ICC transforms and animation are not applied.',
                             '16-bit samples reduced to high byte, straight-alpha RGBA8; cube rules remain caller assertions.'])
    return raw,layout,bindings,report


def build(mods,world,rules,out,apply=False):
    destination=Path(out).absolute()
    if destination.resolve()!=destination:raise ValueError('Output path must have no linked ancestors')
    for source in (mods,world):
        if destination.is_relative_to(Path(source).resolve()):raise ValueError('Output must be outside mods and world')
    if destination.exists():raise ValueError('Output already exists; refusing overwrite')
    if not destination.parent.is_dir():raise ValueError('Output parent must exist')
    raw,layout,bindings,report=prepare(mods,world,rules)
    report.update(readOnly=not apply,destination=str(destination))
    if not apply:return report
    lock=destination.with_name(destination.name+'.atlas-lock');stage=None
    with lock.open('x'):pass
    try:
        if destination.exists():raise ValueError('Output appeared during build')
        stage=Path(tempfile.mkdtemp(prefix='.rule-atlas-',dir=destination.parent))
        (stage/'atlas.png').write_bytes(raw)
        for name,doc in [('atlas.json',layout),('bindings.json',bindings),('report.json',report)]:
            (stage/name).write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding='utf-8')
        for path in stage.iterdir():
            with path.open('r+b') as stream:os.fsync(stream.fileno())
        if destination.exists():raise ValueError('Output appeared during build')
        stage.rename(destination);stage=None
    finally:
        if stage is not None:
            if stage.resolve().parent!=destination.parent.resolve() or not stage.name.startswith('.rule-atlas-'):
                raise ValueError('Unexpected atlas staging cleanup path')
            shutil.rmtree(stage)
        lock.unlink()
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('mods','world','rules','out'):parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--apply',action='store_true');args=parser.parse_args(argv)
    try:result=build(args.mods,args.world,args.rules,args.out,args.apply)
    except (OSError,ValueError,zipfile.BadZipFile) as error:parser.exit(2,str(error)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())

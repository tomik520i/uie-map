"""Build a new offline catalog directory from an existing trusted asset export."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import zlib
import atlas_identity as atlas
import catalog_import_plan
from offline_catalog import Catalog

FILES=('assets.json','atlas-layout.json','atlas.png','grass.png','foliage.png')


def verify_png(raw):
    """CRC, structure, bounded zlib stream and filter bytes; no pixel reconstruction."""
    width,height=atlas._png_size(raw,'PNG')
    depth,color,compression,filtering,interlace=raw[24:29]
    channels={0:1,2:3,6:4}.get(color)
    if depth!=8 or not channels or compression or filtering or interlace:
        raise ValueError('Only non-interlaced 8-bit grey/RGB/RGBA PNG is supported')
    stride=width*channels+1;expected=stride*height
    inflater=zlib.decompressobj();pos=8;total=0;seen=False;ended=False;closed=False
    while pos<len(raw):
        if pos+12>len(raw):raise ValueError('Truncated PNG chunk')
        size=struct.unpack('>I',raw[pos:pos+4])[0];kind=raw[pos+4:pos+8]
        end=pos+12+size
        if end>len(raw):raise ValueError('Truncated PNG data')
        data=raw[pos+8:end-4]
        if zlib.crc32(kind+data)&0xffffffff!=struct.unpack('>I',raw[end-4:end])[0]:raise ValueError('PNG CRC mismatch')
        if kind==b'IHDR' and pos!=8:raise ValueError('Repeated IHDR')
        if kind==b'IDAT':
            if closed:raise ValueError('Noncontiguous IDAT')
            seen=True
            pending=data
            while pending:
                try:decoded=inflater.decompress(pending,min(1024*1024,expected-total+1))
                except zlib.error as error:raise ValueError('Invalid PNG zlib stream') from error
                if total+len(decoded)>expected:raise ValueError('PNG expands beyond dimensions')
                first=(-total)%stride
                if any(decoded[i]>4 for i in range(first,len(decoded),stride)):raise ValueError('Invalid PNG filter')
                total+=len(decoded);pending=inflater.unconsumed_tail
                if inflater.unused_data:raise ValueError('Trailing PNG zlib data')
        elif seen:closed=True
        if kind==b'IEND':
            if size or end!=len(raw):raise ValueError('Invalid PNG ending')
            ended=True;break
        if kind not in (b'IHDR',b'IDAT',b'IEND',b'PLTE') and kind[0]&32==0:raise ValueError('Unsupported critical PNG chunk')
        pos=end
    if not ended or not seen or not inflater.eof or total!=expected:raise ValueError('Incomplete PNG stream')
    return dict(width=width,height=height)


def build(source,destination):
    source=Path(source).resolve();destination=Path(destination).absolute()
    if destination.resolve()!=destination or destination==source or destination.is_relative_to(source):
        raise ValueError('Destination must be separate and have no linked ancestors')
    if destination.exists():raise ValueError('Destination already exists; never overwriting a catalog')
    if not destination.parent.is_dir():raise ValueError('Destination parent must exist')
    lock=destination.with_name(destination.name+'.import-lock')
    with lock.open('x'):
        pass
    stage=None
    try:
        if destination.exists():raise ValueError('Destination already exists')
        stage=Path(tempfile.mkdtemp(prefix='.catalog-',dir=destination.parent))
        hashes={}
        for name in FILES:
            raw=atlas._read(source/name,256*1024*1024 if name.endswith('.png') else 128*1024*1024,name)
            (stage/name).write_bytes(raw);hashes[name]=hashlib.sha256(raw).hexdigest()
        metadata=catalog_import_plan.inspect(stage/'assets.json',stage/'atlas-layout.json')
        layout,digest=atlas.load_layout(stage/'atlas.png',stage/'atlas-layout.json')
        images={name:verify_png((stage/name).read_bytes()) for name in FILES if name.endswith('.png')}
        for name in ('grass.png','foliage.png'):
            if images[name]!=dict(width=256,height=256):raise ValueError('Colormaps must be 256x256')
        compiled=Catalog.compile(stage/'assets.json',stage/'atlas-layout.json',stage/'catalog.json')
        hashes['catalog.json']=hashlib.sha256((stage/'catalog.json').read_bytes()).hexdigest()
        result=dict(format='UIE_CATALOG_PACKAGE1',files=hashes,catalog=compiled,images=images,
                    atlasBindingVerified=True,sourceMetadata=metadata['metadata'],
                    limitations=['Asset-to-atlas semantic compatibility and mod models remain caller responsibility.',
                                 'This imports existing exports, not mod JARs.'])
        (stage/'package.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        for path in stage.iterdir():
            with path.open('r+b') as file:os.fsync(file.fileno())
        if destination.exists():raise ValueError('Destination appeared during import')
        stage.rename(destination);stage=None
        return result
    finally:
        if stage is not None:
            if stage.resolve().parent!=destination.parent.resolve() or not stage.name.startswith('.catalog-'):
                raise ValueError('Unexpected staging cleanup path')
            shutil.rmtree(stage)
        lock.unlink()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True,type=Path);parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--apply',action='store_true',help='Create the new catalog; without this only inspect metadata')
    args=parser.parse_args(argv)
    try:
        result=build(args.source,args.out) if args.apply else catalog_import_plan.inspect(args.source/'assets.json',args.source/'atlas-layout.json')
        result['destination']=str(args.out.absolute())
    except (OSError,ValueError) as error:parser.exit(2,str(error)+'\n')
    print(json.dumps(result,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())

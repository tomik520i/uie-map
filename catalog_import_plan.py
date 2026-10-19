"""Validate exported asset metadata before implementing a portable catalog importer.

Read-only. This is not a JAR importer and does not create a catalog.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path


def uv(value):
    if (not isinstance(value,list) or len(value)!=4 or
        any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in value) or
        value[0]==value[2] or value[1]==value[3]):
        raise ValueError('Invalid UV rectangle: '+repr(value))


def validate(assets,layout):
    if not isinstance(assets,dict) or not isinstance(assets.get('blocks'),dict) or not assets['blocks']:
        raise ValueError('assets.json must contain a non-empty blocks object')
    if not isinstance(layout,dict) or layout.get('format')!='UIE_ATLAS_LAYOUT1':
        raise ValueError('Unsupported atlas layout format')
    registered=layout.get('registered')
    if not isinstance(registered,dict):raise ValueError('Missing registered atlas sprites')
    missing=layout.get('missing')
    if not isinstance(missing,dict) or not isinstance(missing.get('iconName'),str):
        raise ValueError('Missing fallback sprite')
    uv(missing.get('uv'))
    for name,sprite in registered.items():
        if not isinstance(name,str) or not name or not isinstance(sprite,dict):raise ValueError('Invalid sprite')
        uv(sprite.get('uv'))
    variants=0;unregistered=set()
    for name,block in assets['blocks'].items():
        if not isinstance(name,str) or ':' not in name or not isinstance(block,dict):raise ValueError('Invalid block name or data')
        if type(block.get('renderType')) is not int or type(block.get('opaque')) is not bool or not isinstance(block.get('class'),str):
            raise ValueError('Invalid block properties: '+name)
        rows=block.get('variants')
        if not isinstance(rows,list) or not rows:raise ValueError('Missing variants: '+name)
        seen=set()
        for row in rows:
            if not isinstance(row,dict):raise ValueError('Invalid variant: '+name)
            meta=row.get('meta');color=row.get('color',0xffffff)
            if type(meta) is not int or not 0<=meta<=15 or meta in seen:raise ValueError('Invalid or duplicate metadata: '+name)
            # Java exports can include signed ARGB; renderer uses the low 24 bits.
            if type(color) is not int or not -(2**31)<=color<=2**32-1:raise ValueError('Invalid color: '+name)
            seen.add(meta);variants+=1
            faces=row.get('faces')
            if not isinstance(faces,list) or len(faces)!=6:raise ValueError('Expected six faces: '+name)
            for face in faces:
                if face is None:continue
                if not isinstance(face,dict) or not isinstance(face.get('name'),str):raise ValueError('Invalid face: '+name)
                uv(face.get('uv'))
                if face['name'] not in registered and face['name']!=missing['iconName']:unregistered.add(face['name'])
    return dict(blocks=len(assets['blocks']),variants=variants,sprites=len(registered),
                unregisteredFaceNames=sorted(unregistered))


def inspect(assets_path,layout_path):
    documents=[];hashes=[]
    for path in (assets_path,layout_path):
        with Path(path).open('rb') as file:raw=file.read(128*1024*1024+1)
        if len(raw)>128*1024*1024:raise ValueError('Export exceeds 128 MiB inspection limit')
        documents.append(json.loads(raw));hashes.append(hashlib.sha256(raw).hexdigest())
    return dict(format='UIE_CATALOG_IMPORT_PLAN1',readOnly=True,metadata=validate(*documents),
                assetsSha256=hashes[0],layoutSha256=hashes[1],
                limitations=['PNG integrity and atlas binding not verified.',
                             'Model compatibility is not certified; no catalog was created.'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets',required=True,type=Path);parser.add_argument('--layout',required=True,type=Path)
    args=parser.parse_args(argv)
    try:result=inspect(args.assets,args.layout)
    except (OSError,ValueError) as error:parser.exit(2,str(error)+'\n')
    print(json.dumps(result,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())

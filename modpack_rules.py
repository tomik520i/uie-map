"""Bind explicit cube rules to a world registry and validated, pinned JAR textures.

Read-only plan, not a catalog. Unlisted metadata always remains unsupported.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile
import atlas_identity
import modpack_registry
import uiemap_modpack

FORMAT='UIEMAP_MODPACK_RULES1'
HEX=re.compile('[0-9a-f]{64}')


def fields(value, expected, label):
    if not isinstance(value,dict) or set(value)!=set(expected):
        raise ValueError(label+' has missing or unknown fields')


def load(path):
    raw=atlas_identity._read(path,2*1024*1024,'modpack rules')
    doc=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=atlas_identity._no_duplicates,
                   parse_constant=atlas_identity._reject_constant)
    fields(doc,('format','textures','blocks'),'rules')
    if doc['format']!=FORMAT:raise ValueError('Unsupported rules format')
    textures=doc['textures']; blocks=doc['blocks']
    if not isinstance(textures,dict) or not 1<=len(textures)<=4096:raise ValueError('Expected 1..4096 textures')
    for alias,ref in textures.items():
        if not re.fullmatch('[A-Za-z0-9_.-]{1,128}',alias):raise ValueError('Invalid texture alias')
        fields(ref,('archive','entry','sha256'),'texture')
        name=ref['archive']; entry=ref['entry']; sha=ref['sha256']
        if not isinstance(name,str) or '/' in name or '\\' in name or ':' in name or '\0' in name or not name.lower().endswith('.jar'):
            raise ValueError('Archive must be a direct JAR filename')
        if not isinstance(entry,str) or '\\' in entry or '\0' in entry or any(p in ('','..','.') for p in entry.split('/')):
            raise ValueError('Invalid texture entry')
        parts=PurePosixPath(entry).parts
        if len(parts)<5 or parts[0]!='assets' or parts[2]!='textures' or parts[3] not in ('blocks','block') or not entry.lower().endswith('.png'):
            raise ValueError('Texture must be a block PNG entry')
        if not isinstance(sha,str) or not HEX.fullmatch(sha):raise ValueError('Texture SHA256 must pin exact PNG bytes')
    if not isinstance(blocks,list) or not 1<=len(blocks)<=4096:raise ValueError('Expected 1..4096 block rules')
    seen=set(); used=set()
    for block in blocks:
        fields(block,('name','metadata','model','opacity','faces','evidence'),'block rule')
        if not isinstance(block['name'],str) or ':' not in block['name']:raise ValueError('Invalid registry name')
        if block['model']!='cube' or block['opacity'] not in ('solid','cutout'):raise ValueError('Only explicit solid/cutout cube rules are supported')
        metas=block['metadata']
        if not isinstance(metas,list) or not metas or len(metas)>16 or any(type(m) is not int or not 0<=m<=15 for m in metas):
            raise ValueError('Metadata must list exact integers 0..15')
        for meta in metas:
            key=(block['name'],meta)
            if key in seen:raise ValueError('Overlapping metadata rules')
            seen.add(key)
        faces=block['faces']
        if not isinstance(faces,list) or len(faces)!=6 or any(not isinstance(f,str) or f not in textures for f in faces):
            raise ValueError('Six known texture aliases required in D U N S W E order')
        used.update(faces)
        if not isinstance(block['evidence'],str) or not 1<=len(block['evidence'].strip())<=2048:
            raise ValueError('Rule requires an evidence note')
    if used!=set(textures):raise ValueError('Unused texture declarations')
    return doc


def inspect(mods,world,rules):
    doc=load(rules); registry=modpack_registry.read_registry(world)
    names={name:block_id for block_id,name in registry.items()}
    if len(names)!=len(registry):raise ValueError('Ambiguous registry names')
    if any(b['name'] not in names for b in doc['blocks']):raise ValueError('Rule names absent from world registry')
    root=Path(mods).absolute()
    if root.resolve()!=root or not root.is_dir():raise ValueError('mods must be an existing non-linked directory')
    selected={ref['archive'] for ref in doc['textures'].values()}
    if len(selected)>128:raise ValueError('At most 128 selected archives')
    indexed={}; budget={'bytes':0}
    for name in sorted(selected):
        entries={ref['entry'] for ref in doc['textures'].values() if ref['archive']==name}
        _,textures=uiemap_modpack.inspect_jar(root/name,verify_block_textures=True,texture_budget=budget,texture_entries=entries)
        indexed[name]={t['entry']:t for t in textures}
    bound={}
    for alias,ref in sorted(doc['textures'].items()):
        texture=indexed[ref['archive']].get(ref['entry'])
        if texture is None:raise ValueError('Selected texture is absent: '+alias)
        validation=texture.get('validation',{})
        if not validation.get('payloadVerified'):raise ValueError('Selected PNG not verified: '+alias+' ('+validation.get('reason','unknown')+')')
        if validation['sha256']!=ref['sha256']:raise ValueError('Selected PNG hash changed: '+alias)
        if not validation['staticSquareCandidate']:raise ValueError('Only static square texture candidates can bind: '+alias)
        bound[alias]=dict(ref,resource=texture['resource'],width=validation['width'],height=validation['height'],
                          bitDepth=validation['bitDepth'],colorType=validation['colorType'])
    variants=[]
    for block in doc['blocks']:
        for meta in block['metadata']:
            variants.append(dict(id=names[block['name']],name=block['name'],metadata=meta,model=block['model'],
                                 opacity=block['opacity'],faces=block['faces'],evidence=block['evidence'],tier='user-declared'))
    variants.sort(key=lambda v:(v['id'],v['metadata']))
    registry_sha=modpack_registry.compare(registry,[],{})['registrySha256']
    identity=dict(registrySha256=registry_sha,textures=bound,variants=variants)
    digest=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return dict(format='UIEMAP_MODPACK_BINDINGS1',readOnly=True,state='bindings-validated',**identity,
                bindingSha256=digest,unlistedMetadataPolicy='fallback',catalogReady=False,modelSemanticsVerified=False,
                limitations=['Rules and evidence are caller assertions, not observed Java rendering behavior.',
                             'No atlas, pixel conversion or renderer catalog was created.',
                             'Only exact declared metadata is bound; missing variants must not inherit another rule.',
                             'Static candidate excludes mcmeta and nonsquare images; runtime animation is not detected.'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('mods','world','rules'):parser.add_argument('--'+name,required=True,type=Path)
    args=parser.parse_args(argv)
    try:result=inspect(args.mods,args.world,args.rules)
    except (OSError,ValueError,zipfile.BadZipFile,zipfile.LargeZipFile) as error:parser.exit(2,str(error)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())

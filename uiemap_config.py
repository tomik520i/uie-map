"""Validate a portable configuration and plan separate outputs. Never renders or writes.

This is a configuration preview, not yet the renderer entry point. A valid result
does not prove Minecraft version, modpack/catalog compatibility, or dimension mode support.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import world_layout as layout

FORMAT='UIEMAP_CONFIG1'
ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')


class ConfigError(ValueError):
    pass


def fields(value,required,optional=()):
    if not isinstance(value,dict) or not set(required)<=set(value) or set(value)-set(required)-set(optional):
        raise ConfigError('Invalid configuration fields; expected '+','.join(sorted(required)))


def text(value,name,limit=4096):
    if not isinstance(value,str) or not 1<=len(value)<=limit or value.strip()!=value or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ConfigError('Invalid '+name)
    return value


def integer(value,name,low,high):
    if type(value)is not int or not low<=value<=high:raise ConfigError('Invalid '+name)
    return value


def validate(doc,*,base):
    fields(doc,{'format','world','catalog','out','dimensions'},{'limits','web'})
    if doc['format']!=FORMAT:raise ConfigError('Unsupported configuration format')
    fields(doc['world'],{'id','path'})
    world_id=text(doc['world']['id'],'world ID',64)
    if not ID.fullmatch(world_id):raise ConfigError('World ID must use ASCII letters, digits, dot, underscore or hyphen')
    base=Path(base).resolve()
    def path(value):
        p=Path(text(value,'path'));return p if p.is_absolute() else base/p
    world_path=path(doc['world']['path']);catalog_path=path(doc['catalog'])
    layout.plain_directory(world_path);layout.plain_directory(catalog_path)
    world=world_path.resolve();catalog=catalog_path.resolve();out=path(doc['out']).resolve()
    for source in (world,catalog):
        if out==source or out.is_relative_to(source) or source.is_relative_to(out):
            raise ConfigError('Output must be separate from world and catalog (including ancestors)')
    if out.exists():layout.plain_directory(out)
    selected=doc['dimensions']
    if not isinstance(selected,list) or not 1<=len(selected)<=1024:raise ConfigError('Select 1..1024 dimensions explicitly')
    mappings=[];rows=[];seen=set()
    for item in selected:
        fields(item,{'key','title','mode'},{'path','catalog'})
        key=text(item['key'],'dimension key',128)
        if key in seen:raise ConfigError('Duplicate dimension key: '+key)
        seen.add(key)
        if not layout.numeric_key(key) and not layout.CUSTOM.fullmatch(key):raise ConfigError('Invalid dimension key')
        default='.' if key=='0' else 'DIM'+key if layout.numeric_key(key) else None
        value=item.get('path',default)
        if value is None:raise ConfigError('Custom dimensions require an explicit path')
        mappings.append(dict(key=key,path=value))
        title=text(item['title'],'dimension title',120)
        mode=item['mode']
        if mode not in ('surface','caves'):raise ConfigError('Mode must be surface or caves; ceiling mode is not implemented')
        selected_catalog=path(item['catalog']) if 'catalog' in item else catalog
        layout.plain_directory(selected_catalog)
        selected_catalog=selected_catalog.resolve()
        if out==selected_catalog or out.is_relative_to(selected_catalog) or selected_catalog.is_relative_to(out):
            raise ConfigError('Output must be separate from every dimension catalog (including ancestors)')
        rows.append(dict(key=key,title=title,mode=mode,catalog=str(selected_catalog)))
    inventory=layout.discover(world,mappings)
    by_key={x['key']:x for x in inventory['dimensions']}
    limits=doc.get('limits',{});fields(limits,set(),{'workers','memoryMiB','minFreeGiB'})
    limits={k:integer(limits.get(k,default),k,low,high) for k,default,low,high in
            [('workers',1,1,32),('memoryMiB',2048,128,65536),('minFreeGiB',24,20,1024)]}
    web=doc.get('web',{});fields(web,set(),{'host','port'})
    host=text(web.get('host','127.0.0.1'),'web host',253)
    # Literal addresses or DNS names only; this is never a shell command or URL.
    if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:' for c in host):raise ConfigError('Invalid web host')
    web=dict(host=host,port=integer(web.get('port',8211),'web port',1,65535))
    warnings=list(inventory['warnings'])
    for row in rows:
        found=by_key[row['key']]
        namespace=hashlib.sha256(json.dumps([world_id,row['key']],ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
        root=out/('dimension-'+namespace)
        row.update(path=found['path'],regionPath=str(world/found['regionPath']),regions=found['regions'],
                   levelDat=str(world/'level.dat'),outputs={name:str(root/name) for name in ('surface','caves','collisions')})
        if row['key']=='-1' and row['mode']=='surface':
            warnings.append(dict(path=found['path'],reason='Nether surface below the roof is not implemented; do not treat this preview as a supported render'))
    return dict(format=FORMAT,world=dict(id=world_id,path=str(world)),catalog=str(catalog),out=str(out),
                dimensions=rows,limits=limits,web=web,warnings=warnings,renderReady=False,readOnly=True)


def load(path):
    path=Path(path)
    with path.open('rb') as f:raw=f.read(65537)
    if len(raw)>65536:raise ConfigError('Configuration exceeds 64 KiB')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ConfigError('Duplicate JSON key: '+key)
            result[key]=value
        return result
    def constant(value):raise ConfigError('Non-finite JSON value: '+value)
    doc=json.loads(raw,object_pairs_hook=pairs,parse_constant=constant)
    return validate(doc,base=path.resolve().parent)


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('config',type=Path);a=ap.parse_args(argv)
    try:result=load(a.config)
    except (OSError,ValueError) as e:ap.exit(2,str(e)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())

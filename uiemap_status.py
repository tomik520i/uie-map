"""Read recorded render progress and independently check publication metadata."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import time
import atlas_identity
import dimension_binding as binding
import uiemap_config
from uiemap_serve import HEX,TILE


def read(path,limit):
    path=Path(path)
    if path.resolve()!=path:raise ValueError('Linked metadata paths are not allowed')
    raw=atlas_identity._read(path,limit,'status metadata')
    doc=json.loads(raw,object_pairs_hook=atlas_identity._no_duplicates,parse_constant=atlas_identity._reject_constant)
    if not isinstance(doc,dict):raise ValueError('Expected a JSON object')
    return doc,raw


def publication(store,world,dimension,mode):
    if not (store/'current.json').exists():return dict(state='unpublished')
    pointer,_=read(store/'current.json',65536);sha=pointer.get('indexSha256')
    if not isinstance(sha,str) or not HEX.fullmatch(sha):raise ValueError('Invalid index pointer')
    doc,raw=read(store/'indexes'/(sha+'.json'),64*1024**2)
    if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('Index hash mismatch')
    context=doc.get('context')
    if not isinstance(context,dict) or context.get('world')!=world or context.get('dimension')!=dimension or context.get('caves')is not (mode=='caves'):
        raise ValueError('Published world/dimension/mode mismatch')
    tiles=doc.get('tiles')
    if not isinstance(tiles,dict) or not tiles:raise ValueError('Empty or invalid published tiles')
    for key,value in tiles.items():
        if not TILE.fullmatch(key) or not isinstance(value,str) or not HEX.fullmatch(value):raise ValueError('Invalid tile reference')
    result=dict(state='published',indexSha256=sha,tiles=len(tiles),payloadsVerified=False)
    if mode=='caves':
        from cave_pair_publish import _published_document
        caps=_published_document(store/'cave-data')
        if caps is None:
            return dict(result,state='awaiting-caps',reason='No companion cap index')
        cap_sha,cap=caps
        if (cap['geometryIndexSha256']!=sha or cap['rendererIdentity']!=doc.get('rendererIdentity')
            or cap['atlasSha256']!=doc.get('atlasSha256') or set(cap['pairs'])!=set(tiles)):
            return dict(result,state='awaiting-caps',reason='Companion cap index does not match geometry')
        result.update(capIndexSha256=cap_sha,capPairs=len(cap['pairs']))
    return result


def progress(store,now):
    if not (store/'status.json').exists():return dict(state='unknown',processVerified=False)
    doc,_=read(store/'status.json',1024*1024)
    if doc.get('format')!='UIE_HIRES_STATUS1':raise ValueError('Unsupported render status format')
    state=doc.get('state')
    if not isinstance(state,str):raise ValueError('Missing render state')
    result=dict(state=state,processVerified=False)
    for key in ('total','done','plannedJobs','skippedUnchanged','notScheduled','failedJobs','failedTiles'):
        if key in doc:
            if type(doc[key]) is not int or doc[key]<0:raise ValueError('Invalid progress count: '+key)
            result[key]=doc[key]
    for key in ('startedAt','updatedAt','finishedAt'):
        value=doc.get(key)
        if value is not None:
            if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise ValueError('Invalid progress time')
            result[key]=value
    updated=result.get('updatedAt')
    result['heartbeatAgeSeconds']=None if updated is None else max(0,now-updated)
    if state=='running':
        result['state']='running-reported' if updated is not None and 0<=now-updated<=120 else 'stale-running-record'
    if 'planComplete' in doc:
        if type(doc['planComplete']) is not bool:raise ValueError('Invalid planComplete')
        result['planComplete']=doc['planComplete']
    result['hasFailures']=bool(result.get('failedJobs',0) or result.get('failedTiles',0))
    return result


def inspect(config_path,now=None):
    config=uiemap_config.load(config_path);now=time.time() if now is None else now;rows=[]
    for dimension in config['dimensions']:
        row=dict(key=dimension['key'],title=dimension['title'],mode=dimension['mode'])
        store=Path(dimension['outputs'][dimension['mode']]);row['store']=str(store)
        try:
            if store.resolve()!=store:raise ValueError('Linked output ancestors are not allowed')
            if not store.exists():
                row.update(publication=dict(state='unpublished'),render=dict(state='not-started',processVerified=False))
            else:
                expected=binding.make_binding(world=config['world']['path'],world_id=config['world']['id'],
                    dimension=dimension['key'],dimension_path=dimension['path'],mode=dimension['mode'])
                binding.ensure(store,expected)
                # A failed render must not hide the last valid published generation.
                for name,action in [('publication',lambda:publication(store,config['world']['id'],dimension['key'],dimension['mode'])),
                                    ('render',lambda:progress(store,now))]:
                    try:row[name]=action()
                    except (OSError,ValueError) as error:row[name]=dict(state='error',error=str(error))
        except (OSError,ValueError) as error:row.update(error=str(error))
        rows.append(row)
    return dict(format='UIEMAP_STATUS1',readOnly=True,observedAt=now,world=config['world']['id'],dimensions=rows,
                limitations=['Recorded heartbeat does not prove a process is alive.',
                             'Publication checks index identity and hash, not tile payloads or current world freshness.'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('config',type=Path);args=parser.parse_args(argv)
    try:result=inspect(args.config)
    except (OSError,ValueError) as error:parser.exit(2,str(error)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 2 if any('error' in row or any(row.get(key,{}).get('state')=='error' for key in ('publication','render')) for row in result['dimensions']) else 0


if __name__=='__main__':raise SystemExit(main())

"""Build a web catalog from configured, published surface dimensions.

Default: read-only plan. --apply writes a new immutable web configuration and
starts this checkout's Node viewer in the foreground. No PM2 changes or renders.
"""
import argparse,hashlib,json,os,re,shutil,subprocess,tempfile
from pathlib import Path
import dimension_binding as binding
import uiemap_config as config

HEX=re.compile(r'[0-9a-f]{64}\Z')
TILE=re.compile(r'(0|-?[1-9][0-9]{0,6}),(0|-?[1-9][0-9]{0,6})\Z')


def read_json(path,limit):
    with Path(path).open('rb') as f:raw=f.read(limit+1)
    if len(raw)>limit:raise ValueError('Metadata exceeds size limit')
    return json.loads(raw),raw


def snapshot(config_file,main_dimension):
    doc=config.load(config_file);out=Path(doc['out']);views=[];files={};routes=set()
    if len(doc['dimensions'])>33:raise ValueError('The web catalog supports at most 33 dimensions')
    if main_dimension not in [row['key'] for row in doc['dimensions']]:raise ValueError('Main dimension is not selected')
    for row in doc['dimensions']:
        if row['mode']!='surface':raise ValueError('Web configuration currently supports surface dimensions only')
        store=Path(row['outputs']['surface'])
        if store.resolve()!=store:raise ValueError('Linked output ancestors are not allowed')
        expected=binding.make_binding(world=doc['world']['path'],world_id=doc['world']['id'],
            dimension=row['key'],dimension_path=row['path'],mode='surface')
        binding.ensure(store,expected)
        pointer,_=read_json(store/'current.json',65536);sha=pointer.get('indexSha256')
        if not isinstance(sha,str) or not HEX.fullmatch(sha):raise ValueError('Invalid published index pointer')
        index,raw=read_json(store/'indexes'/(sha+'.json'),64*1024**2)
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('Published index hash mismatch')
        context=index.get('context',{})
        if context.get('world')!=doc['world']['id'] or context.get('dimension')!=row['key'] or context.get('caves')is not False:
            raise ValueError('Published index belongs to a different world, dimension or mode')
        tiles=index.get('tiles')
        if not isinstance(tiles,dict) or not tiles:raise ValueError('Published dimension contains no tiles')
        coords=[]
        for key,object_sha in tiles.items():
            match=TILE.fullmatch(key)
            if not match or not isinstance(object_sha,str) or not HEX.fullmatch(object_sha):raise ValueError('Invalid published tile')
            x,z=map(int,match.groups());coords.append((x,z))
        bounds=dict(min=[min(x for x,z in coords)*64,0,min(z for x,z in coords)*64],
                    max=[(max(x for x,z in coords)+1)*64,256,(max(z for x,z in coords)+1)*64])
        if any(abs(bounds[side][axis])>30000000 for side in ('min','max') for axis in (0,2)):
            raise ValueError('Published bounds exceed supported world coordinates')
        x,z=min(coords,key=lambda p:(p[0]*p[0]+p[1]*p[1],p))
        view=dict(format='UIE_MAP_VIEW1',world=doc['world']['id'],dimension=row['key'],title=row['title'],
                  bounds=bounds,focus=[x*64+32,80,z*64+32])
        key='d-'+hashlib.sha256(row['key'].encode()).hexdigest()[:20]
        if key in routes:raise ValueError('Dimension route collision')
        routes.add(key)
        filename=key+'.json';files[filename]=view
        views.append(dict(key=key,filename=filename,hiresStore=str(store),dimension=row['key'],indexSha256=sha))
    identity=json.dumps(dict(files=files,views=views,main=main_dimension,web=doc['web']),sort_keys=True,separators=(',',':')).encode()
    stage=out/('web-config-'+hashlib.sha256(identity).hexdigest())
    main=next(view for view in views if view['dimension']==main_dimension)
    menu=dict(format='UIE_DIMENSION_VIEWS1',mainTitle=files[main['filename']]['title'],views=[
        dict(key=v['key'],viewFile=str(stage/v['filename']),hiresStore=v['hiresStore']) for v in views if v is not main])
    files['dimensions.json']=menu
    env=dict(HOST=doc['web']['host'],PORT=str(doc['web']['port']),UIE_EXTRA_PORTS='',
        UIE_MAP_VIEW_FILE=str(stage/main['filename']),UIE_HIRES_STORE=main['hiresStore'],UIE_DIMENSIONS_FILE=str(stage/'dimensions.json'))
    return dict(format='UIEMAP_WEB_PLAN1',directory=str(stage),files=files,environment=env,
        dimensions=[dict(key=v['dimension'],indexSha256=v['indexSha256']) for v in views],readOnly=True)


def write_snapshot(plan):
    stage=Path(plan['directory'])
    if stage.resolve()!=stage:raise ValueError('Web configuration path was redirected')
    expected={name:json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode() for name,value in plan['files'].items()}
    if stage.exists():
        if set(p.name for p in stage.iterdir())!=set(expected):raise ValueError('Existing web configuration differs')
        for name,raw in expected.items():
            p=stage/name
            if p.is_symlink() or p.read_bytes()!=raw:raise ValueError('Existing web configuration differs')
        return stage
    # Parent is the already existing configured output with verified stores.
    temp=Path(tempfile.mkdtemp(prefix='.web-config-',dir=stage.parent))
    for name,raw in expected.items():
        with (temp/name).open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    temp.rename(stage)
    return stage


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('config',type=Path)
    parser.add_argument('--main-dimension',required=True);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args(argv)
    try:
        plan=snapshot(args.config,args.main_dimension)
        if not args.apply:print(json.dumps(plan,ensure_ascii=False,indent=2));return 0
        node=shutil.which('node')
        if not node:raise ValueError('Node.js is required to serve the viewer')
        stage=write_snapshot(plan)
        env={k:v for k,v in os.environ.items() if not k.startswith('UIE_')};env.update(plan['environment'])
        print(json.dumps(dict(state='starting-web',configuration=str(stage),host=env['HOST'],port=env['PORT'])),flush=True)
        return subprocess.run([node,str(Path(__file__).with_name('server.mjs'))],env=env).returncode
    except (OSError,ValueError) as error:parser.exit(2,str(error)+'\n')


if __name__=='__main__':raise SystemExit(main())

"""Export block collisions for one published configured surface dimension.

Read-only plan by default. --apply runs one bounded-memory Linux systemd worker.
Source regions come from the selected dimension; block registry from root level.dat.
No web changes, Minecraft client, scheduler installation or garbage collection.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid
import dimension_binding as binding
import uiemap_config
import uiemap_render
import uiemap_status


def plan(config_path,dimension):
    surface=uiemap_render.plan(config_path,dimension)
    if surface['mode']!='surface':raise ValueError('Block collisions require a surface configuration')
    doc=uiemap_config.load(surface['config'])
    if hashlib.sha256(Path(surface['config']).read_bytes()).hexdigest()!=surface['configSha256']:
        raise ValueError('Configuration changed during collision planning')
    row=next(r for r in doc['dimensions'] if r['key']==dimension)
    publication=uiemap_status.publication(Path(surface['out']),doc['world']['id'],dimension,'surface')
    if publication['state']!='published':raise ValueError('Surface must be published before collision export')
    out=Path(row['outputs']['collisions'])
    if out.resolve()!=out:raise ValueError('Linked collision output ancestors are not allowed')
    expected=binding.make_binding(world=doc['world']['path'],world_id=doc['world']['id'],dimension=dimension,
        dimension_path=row['path'],mode='collisions')
    if out.exists():binding.ensure(out,expected)
    return dict(format='UIEMAP_COLLISION_PLAN1',config=surface['config'],configSha256=surface['configSha256'],
        world=doc['world']['id'],dimension=dimension,mode='collisions',worldPath=doc['world']['path'],
        regionWorld=str(Path(row['regionPath']).parent),catalog=doc['catalog'],surface=surface['out'],
        out=str(out),binding=expected,limits=doc['limits'],publication=publication,readOnly=True,webChanged=False)


def make_tools(work):
    import collision_region_probe as probe
    import collision_export as export
    class DimensionToolkit(probe.Toolkit):
        def __init__(self):
            super().__init__(Path(work['catalog']),read_index=lambda store:probe.read_visible(store,world=work['world'],dimension=work['dimension']))
            # DIM*/level.dat is not the authoritative mod registry.
            self.load_registry=lambda unused_region_world:export.read_registry(Path(work['worldPath']))
        def fresh(self):return DimensionToolkit()
    return DimensionToolkit()


def run_worker(work):
    if os.name!='posix' or not os.environ.get('INVOCATION_ID'):raise ValueError('Worker requires Linux user systemd')
    import collision_export as export
    import collision_world_runner as runner
    uiemap_render.free_space(work)
    out=export.check_paths(Path(work['out']),world=Path(work['worldPath']),hires=Path(work['surface']),catalog=Path(work['catalog']))
    if out.exists():binding.ensure(out,work['binding'])
    else:
        out.mkdir(parents=True);binding.ensure(out,work['binding'],initialize=True)
    lock=export._lock(out)
    if lock is None:raise ValueError('Another collision exporter holds the output lock')
    stopping=threading.Event()
    def guard():
        while not stopping.wait(1):
            try:uiemap_render.free_space(work)
            except (OSError,ValueError):os._exit(75)
    thread=threading.Thread(target=guard,daemon=True);thread.start()
    try:
        binding.ensure(out,work['binding'])
        result=runner.run_pass(out=out,world=Path(work['regionWorld']),hires_store=Path(work['surface']),
            tools=make_tools(work),min_free_bytes=work['limits']['minFreeGiB']*1024**3)
        if result['state']!='complete':raise ValueError('Collision export incomplete: '+json.dumps(result))
        return dict(format='UIEMAP_COLLISION_RESULT1',world=work['world'],dimension=work['dimension'],
            out=str(out),store=str(out/'store'),result=result,webChanged=False)
    finally:stopping.set();lock.close()


def service_command(work,unit):
    args=uiemap_render.service_command(work,unit)
    args[args.index(str(Path(uiemap_render.__file__).resolve()))]=str(Path(__file__).resolve())
    return args


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('config',type=Path)
    parser.add_argument('--dimension',required=True);parser.add_argument('--apply',action='store_true')
    parser.add_argument('--worker-config-sha',help=argparse.SUPPRESS);args=parser.parse_args(argv)
    try:
        work=plan(args.config,args.dimension)
        if args.worker_config_sha:
            if args.apply or args.worker_config_sha!=work['configSha256']:raise ValueError('Configuration changed before execution')
            print(json.dumps(run_worker(work)));return 0
        if not args.apply:print(json.dumps(work,indent=2));return 0
        if os.name!='posix' or not shutil.which('systemd-run'):raise ValueError('Execution requires Linux with user systemd')
        uiemap_render.free_space(work);unit='uiemap-render-'+uuid.uuid4().hex+'.service'
        try:return subprocess.run(service_command(work,unit)).returncode
        except KeyboardInterrupt:
            subprocess.run(['systemctl','--user','stop',unit],check=False);raise
    except (OSError,ValueError) as error:parser.exit(2,str(error)+'\n')


if __name__=='__main__':raise SystemExit(main())

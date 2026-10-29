"""Run one configured surface or paired cave dimension under a Linux user-systemd memory limit.

Without --apply this only prints the selected command and resource limits.
Publication is explicit in the result; this does not configure or restart a web server.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import uiemap_config as config
import dimension_binding as binding


def plan(config_path, dimension):
    config_path=Path(config_path).resolve()
    with config_path.open('rb') as f:raw=f.read(65537)
    if len(raw)>65536:raise ValueError('Configuration exceeds 64 KiB')
    doc=config.load(config_path)
    with config_path.open('rb') as f:
        if f.read(65537)!=raw:raise ValueError('Configuration changed during planning')
    rows=[row for row in doc['dimensions'] if row['key']==dimension]
    if len(rows)!=1:raise ValueError('Select a dimension explicitly present in the configuration')
    row=rows[0]
    if dimension=='-1':raise ValueError('Configured Nether rendering is not implemented')
    if row['regions']==0:raise ValueError('Selected dimension has no saved region files')
    mode=row['mode'];out=Path(row['outputs'][mode])
    if out.resolve()!=out:raise ValueError('Linked output ancestors are not allowed')
    expected=binding.make_binding(world=doc['world']['path'],world_id=doc['world']['id'],
                                  dimension=dimension,dimension_path=row['path'],mode=mode)
    if out.exists():binding.ensure(out,expected)
    command=[sys.executable,'-B',str(Path(__file__).with_name('world_hires.py')),
        '--world',doc['world']['path'],'--catalog-dir',row['catalog'],'--out',str(out),
        '--world-id',doc['world']['id'],'--dimension-key',dimension,'--dimension-path',row['path'],
        '--workers',str(doc['limits']['workers']),'--prepare-immutable']
    if mode=='caves':command+=['--caves','--cave-caps']
    return dict(format='UIEMAP_RENDER_PLAN1',config=str(config_path),configSha256=hashlib.sha256(raw).hexdigest(),
        world=doc['world']['id'],dimension=dimension,mode=mode,out=str(out),command=command,limits=doc['limits'],
        binding=expected,regions=row['regions'],readOnly=True,webChanged=False)


def prepare_output(plan):
    import hires_bundle as bundle
    out=Path(plan['out'])
    if out.resolve()!=out:raise ValueError('Output path changed after planning')
    if out.exists():binding.ensure(out,plan['binding'])
    else:
        out.mkdir(parents=True)
        binding.ensure(out,plan['binding'],initialize=True)
    marker=out/bundle.OWNED_MARKER
    if not marker.exists():
        with marker.open('x',encoding='utf-8') as f:f.write('Owned configured UIE surface store\n')
    bundle._owned(out)
    return out


def free_space(plan):
    ancestor=Path(plan['out'])
    while not ancestor.exists():ancestor=ancestor.parent
    free=shutil.disk_usage(ancestor).free
    if free < plan['limits']['minFreeGiB']*1024**3:
        raise ValueError('Free disk space is below the configured reserve')
    return free


def service_command(plan, unit):
    if not unit.startswith('uiemap-render-') or not unit.endswith('.service'):
        raise ValueError('Invalid owned unit name')
    return ['systemd-run','--user','--wait','--pipe','--collect','--service-type=exec','--unit='+unit,
        '--property=MemoryMax='+str(plan['limits']['memoryMiB'])+'M','--property=KillMode=control-group',
        '--property=CPUWeight=10','--property=IOWeight=10','--property=Nice=15',
        sys.executable,'-B',str(Path(__file__).resolve()),plan['config'],'--dimension',plan['dimension'],
        '--worker-config-sha',plan['configSha256']]


def publication_receipt(plan,result):
    import re
    valid=lambda value:isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None
    if result.get('published') is not True or not valid(result.get('indexSha256')):
        raise ValueError('Publisher did not confirm geometry publication')
    receipt=dict(format='UIEMAP_RENDER_RESULT1',world=plan['world'],dimension=plan['dimension'],
                 mode=plan.get('mode','surface'),out=plan['out'],published=True,indexSha256=result['indexSha256'],webChanged=False)
    if plan.get('mode')=='caves':
        cap=result.get('capPublication')
        if not isinstance(cap,dict) or cap.get('published') is not True or not valid(cap.get('indexSha256')):
            raise ValueError('Cave geometry published but companion caps are not confirmed; rerun to recover')
        receipt['capIndexSha256']=cap['indexSha256']
    return receipt


def run_worker(plan):
    # The worker and every renderer child belong to the same systemd cgroup.
    # If the reserve is crossed, exiting the worker makes systemd stop its children.
    if os.name!='posix' or not os.environ.get('INVOCATION_ID'):
        raise ValueError('Worker requires a Linux systemd service')
    import threading
    import hires_publisher as publisher
    from hires_publish_session import PublishSession
    free_space(plan)
    out=prepare_output(plan)
    stopping=threading.Event()
    def guard():
        while not stopping.wait(1):
            try:free_space(plan)
            except (OSError,ValueError):
                print('Disk reserve crossed; stopping the render service.',file=sys.stderr,flush=True)
                os._exit(75)
    thread=threading.Thread(target=guard,daemon=True);thread.start()
    try:
        # Output remains under the renderer's binding + build lock rules.
        completed=subprocess.run(plan['command'],capture_output=True,text=True)
        if completed.returncode:raise ValueError('Renderer failed: '+completed.stderr[-2000:])
        state=json.loads(completed.stdout)
        if not state.get('planComplete') or state.get('failedJobs') or state.get('failedTiles'):
            raise ValueError('Renderer did not complete the selected dimension; nothing published')
        preparation=state.get('immutablePreparation',{})
        if preparation.get('state') in ('error','skipped'):
            raise ValueError('Geometry preparation failed; nothing published: '+str(preparation.get('message',preparation.get('reason','unknown reason')))[:500])
        binding.ensure(out,plan['binding'])
        session=PublishSession()
        for _ in range(100000):
            free_space(plan)
            result=publisher.run_once(out,session,world=plan['world'],dimension=plan['dimension'],cave_pairs=plan.get('mode')=='caves')
            if result.get('published'):
                return publication_receipt(plan,result)
            if result.get('state')=='error':raise ValueError('Publisher rejected the output')
            time.sleep(.05)
        raise ValueError('Publisher tick limit reached; output retained for retry')
    finally:stopping.set()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config',type=Path);parser.add_argument('--dimension',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--expected-config-sha',help=argparse.SUPPRESS)
    parser.add_argument('--worker-config-sha',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    try:
        work=plan(args.config,args.dimension)
        if args.expected_config_sha and args.expected_config_sha!=work['configSha256']:
            raise ValueError('Configuration changed after update planning')
        if args.worker_config_sha:
            if args.apply or args.worker_config_sha!=work['configSha256']:raise ValueError('Configuration changed before execution')
            print(json.dumps(run_worker(work),ensure_ascii=False));return 0
        if not args.apply:
            print(json.dumps(work,ensure_ascii=False,indent=2));return 0
        if os.name!='posix' or not shutil.which('systemd-run'):raise ValueError('Execution requires Linux with user systemd')
        free_space(work)
        unit='uiemap-render-'+uuid.uuid4().hex+'.service'
        print(json.dumps(dict(state='starting',unit=unit,dimension=work['dimension'])),flush=True)
        try:return subprocess.run(service_command(work,unit)).returncode
        except KeyboardInterrupt:
            subprocess.run(['systemctl','--user','stop',unit],check=False)
            raise
    except (OSError,ValueError) as error:parser.exit(2,str(error)+'\n')


if __name__=='__main__':raise SystemExit(main())

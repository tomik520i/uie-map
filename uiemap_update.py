"""Plan or execute one sequential update pass for explicitly configured surface dimensions."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uiemap_config
import uiemap_render
import uiemap_gc
from uiemap_serve import HEX


def digest(path):
    with Path(path).open('rb') as file:raw=file.read(65537)
    if len(raw)>65536:raise ValueError('Configuration exceeds 64 KiB')
    return hashlib.sha256(raw).hexdigest()


def plan(path):
    path=Path(path).resolve();sha=digest(path);config=uiemap_config.load(path)
    jobs=[uiemap_render.plan(path,row['key']) for row in config['dimensions']]
    if digest(path)!=sha or any(job['configSha256']!=sha for job in jobs):raise ValueError('Configuration changed while planning')
    return dict(format='UIEMAP_UPDATE_PLAN1',config=str(path),configSha256=sha,
                world=config['world']['id'],jobs=jobs,readOnly=True,concurrency=1,webChanged=False)


def execute(work,runner=None,*,cleanup=False):
    runner=subprocess.run if runner is None else runner
    completed=[]
    for index,job in enumerate(work['jobs']):
        if digest(work['config'])!=work['configSha256']:
            raise ValueError('Configuration changed between dimensions; stopping update')
        command=[sys.executable,'-B',str(Path(__file__).with_name('uiemap_render.py')),
                 work['config'],'--dimension',job['dimension'],'--apply','--expected-config-sha',work['configSha256']]
        run=runner(command,capture_output=True,text=True)
        # A zero exit without an explicit publication receipt is not success.
        receipt=None
        for line in run.stdout.splitlines():
            try:value=json.loads(line)
            except ValueError:continue
            if isinstance(value,dict) and value.get('format')=='UIEMAP_RENDER_RESULT1':receipt=value
        valid=(run.returncode==0 and receipt is not None and receipt.get('published') is True
               and receipt.get('world')==work['world'] and receipt.get('dimension')==job['dimension']
               and receipt.get('out')==job['out'] and isinstance(receipt.get('indexSha256'),str)
               and HEX.fullmatch(receipt['indexSha256']) is not None)
        completed.append(dict(dimension=job['dimension'],published=valid,returncode=run.returncode,
                              receipt=receipt if valid else None))
        if not valid:
            return dict(format='UIEMAP_UPDATE_RESULT1',complete=False,results=completed,
                        pending=[j['dimension'] for j in work['jobs'][index+1:]],
                        error=run.stderr[-2000:] or 'Renderer did not confirm publication',webChanged=False)
        if cleanup:
            try:
                completed[-1]['collection']=uiemap_gc.collect(work['config'],job['dimension'],apply=True,
                    expected_config_sha=work['configSha256'])['collection']
            except (OSError,ValueError,RuntimeError) as error:
                return dict(format='UIEMAP_UPDATE_RESULT1',complete=False,results=completed,
                            pending=[j['dimension'] for j in work['jobs'][index+1:]],
                            error='Publication succeeded but collection failed: '+str(error),webChanged=False)
    return dict(format='UIEMAP_UPDATE_RESULT1',complete=True,results=completed,pending=[],webChanged=False)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('config',type=Path)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--gc',action='store_true',help='After publication collect one bounded batch per dimension')
    args=parser.parse_args(argv)
    try:
        work=plan(args.config)
        work['collectAfterPublication']=args.gc
        result=execute(work,cleanup=args.gc) if args.apply else work
    except (OSError,ValueError) as error:parser.exit(2,str(error)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result.get('readOnly') or result.get('complete') else 2


if __name__=='__main__':raise SystemExit(main())

"""One bounded collection in one explicitly selected configured surface store.

Default deletes nothing. The underlying collector may create its build-lock file.
"""
import argparse
import json
import hashlib
from pathlib import Path
import dimension_binding as binding
import hires_gc
import uiemap_config


def collect(config_path,dimension,*,apply=False,max_files=128,max_bytes=64*1024*1024,expected_config_sha=None):
    def check_config():
        if expected_config_sha is not None:
            with Path(config_path).open('rb') as file:raw=file.read(65537)
            if len(raw)>65536 or hashlib.sha256(raw).hexdigest()!=expected_config_sha:
                raise ValueError('Configuration changed before collection')
    check_config()
    config=uiemap_config.load(config_path)
    check_config()
    rows=[row for row in config['dimensions'] if row['key']==dimension]
    if len(rows)!=1:raise ValueError('Select one explicitly configured dimension')
    row=rows[0]
    if row['mode']!='surface':raise ValueError('Configured GC currently supports surface stores only')
    store=Path(row['outputs']['surface'])
    if store.resolve()!=store:raise ValueError('Linked output ancestors are not allowed')
    expected=binding.make_binding(world=config['world']['path'],world_id=config['world']['id'],
        dimension=dimension,dimension_path=row['path'],mode='surface')
    binding.ensure(store,expected)
    result=hires_gc.collect(store,execute=apply,max_files=max_files,max_bytes=max_bytes)
    return dict(format='UIEMAP_GC_RESULT1',world=config['world']['id'],dimension=dimension,
                store=str(store),collection=result,
                notes=['No world files, source catalog or legacy mutable tiles are removed.',
                       'Reported bytes are logical file sizes, not guaranteed recovered disk space.',
                       'Dry run deletes nothing but may create the build-lock file.'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('config',type=Path)
    parser.add_argument('--dimension',required=True);parser.add_argument('--apply',action='store_true')
    parser.add_argument('--max-files',type=int,default=128);parser.add_argument('--max-bytes',type=int,default=64*1024*1024)
    args=parser.parse_args(argv)
    try:result=collect(args.config,args.dimension,apply=args.apply,max_files=args.max_files,max_bytes=args.max_bytes)
    except (OSError,ValueError,RuntimeError) as error:parser.exit(2,str(error)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())

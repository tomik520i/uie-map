"""Bounded companion-only cave cleanup from an explicit configuration.

This collects cave-data indexes, pairs, masks and hardlink aliases only.
Root geometry, source world and catalog are not collected by this command.
"""
import argparse
import json
from pathlib import Path
import cave_gc
import dimension_binding as binding
import uiemap_config


def collect(config_path,dimension,*,apply=False,max_files=64,max_bytes=16*1024*1024):
    config=uiemap_config.load(config_path)
    rows=[row for row in config['dimensions'] if row['key']==dimension]
    if len(rows)!=1:raise ValueError('Select one explicitly configured dimension')
    row=rows[0]
    if row['mode']!='caves':raise ValueError('Companion cleanup requires a cave configuration')
    store=Path(row['outputs']['caves'])
    if store.resolve()!=store:raise ValueError('Linked output ancestors are not allowed')
    expected=binding.make_binding(world=config['world']['path'],world_id=config['world']['id'],
        dimension=dimension,dimension_path=row['path'],mode='caves')
    binding.ensure(store,expected)
    result=cave_gc.collect(store,execute=apply,max_files=max_files,max_bytes=max_bytes)
    return dict(format='UIEMAP_CAVE_GC_RESULT1',world=config['world']['id'],dimension=dimension,
        store=str(store),scope='cave-companion-only',collection=result,
        notes=['Root geometry and its history are not collected.',
               'No source world, catalog or client artifacts are removed.',
               'Dry run deletes nothing but may create the build-lock file.',
               'Logical byte counts do not guarantee recovered disk space.'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('config',type=Path)
    parser.add_argument('--dimension',required=True);parser.add_argument('--apply',action='store_true')
    parser.add_argument('--max-files',type=int,default=64);parser.add_argument('--max-bytes',type=int,default=16*1024*1024)
    args=parser.parse_args(argv)
    try:result=collect(args.config,args.dimension,apply=args.apply,max_files=args.max_files,max_bytes=args.max_bytes)
    except cave_gc.PartialCollectionError as error:
        print(json.dumps(error.report),flush=True);return 2
    except (OSError,ValueError,RuntimeError) as error:parser.exit(2,str(error)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())

"""UIE Map prototype: check configuration, render a surface, or serve published maps."""
import argparse
import sys
from pathlib import Path
import uiemap_config
import uiemap_render
import uiemap_serve
import uiemap_doctor
import catalog_package
import uiemap_status
import uiemap_update
import uiemap_schedule
import uiemap_gc
import uiemap_services
import uiemap_cave_gc
import uiemap_collisions


def main(argv=None):
    # JSON and Czech titles must also work when Windows redirects a cp1252 console.
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    check=commands.add_parser('check',help='Validate configuration without writing or starting processes')
    check.add_argument('config',type=Path)
    status=commands.add_parser('status',help='Show recorded progress and independently verified publication metadata')
    status.add_argument('config',type=Path)
    services=commands.add_parser('services',help='Read actual systemd timer/service state for a configuration path')
    services.add_argument('config',type=Path)
    update=commands.add_parser('update',help='Plan or run one sequential update pass of configured dimensions')
    update.add_argument('config',type=Path);update.add_argument('--apply',action='store_true')
    update.add_argument('--gc',action='store_true')
    schedule=commands.add_parser('schedule',help='Plan or install a user-systemd update timer')
    schedule.add_argument('config',type=Path);schedule.add_argument('--interval',type=int,default=900)
    schedule.add_argument('--apply',action='store_true')
    schedule.add_argument('--gc',action='store_true')
    gc=commands.add_parser('gc',help='Preview or collect one bounded batch of unused surface objects')
    gc.add_argument('config',type=Path);gc.add_argument('--dimension',required=True)
    gc.add_argument('--apply',action='store_true');gc.add_argument('--max-files',type=int,default=128)
    gc.add_argument('--max-bytes',type=int,default=64*1024*1024)
    cave_gc=commands.add_parser('cave-gc',help='Collect only old cave companion data; root geometry is excluded')
    cave_gc.add_argument('config',type=Path);cave_gc.add_argument('--dimension',required=True)
    cave_gc.add_argument('--apply',action='store_true');cave_gc.add_argument('--max-files',type=int,default=64)
    cave_gc.add_argument('--max-bytes',type=int,default=16*1024*1024)
    doctor=commands.add_parser('doctor',help='Report missing prerequisites without rendering or starting services')
    doctor.add_argument('config',type=Path)
    catalog=commands.add_parser('catalog',help='Prepare a new catalog from an existing asset export')
    catalog.add_argument('--source',required=True,type=Path);catalog.add_argument('--out',required=True,type=Path)
    catalog.add_argument('--apply',action='store_true')
    render=commands.add_parser('render',help='Plan or run one configured surface/cave dimension (Linux systemd)')
    render.add_argument('config',type=Path);render.add_argument('--dimension',required=True)
    render.add_argument('--apply',action='store_true',help='Actually run render and publication')
    collisions=commands.add_parser('collisions',help='Export block collisions for one configured surface dimension')
    collisions.add_argument('config',type=Path);collisions.add_argument('--dimension',required=True)
    collisions.add_argument('--apply',action='store_true')
    serve=commands.add_parser('serve',help='Plan or start the web viewer for published surface stores')
    serve.add_argument('config',type=Path);serve.add_argument('--main-dimension',required=True)
    serve.add_argument('--cave-config',type=Path)
    serve.add_argument('--collisions',action='store_true')
    serve.add_argument('--apply',action='store_true',help='Write web configuration and start Node in foreground')
    args=parser.parse_args(argv)
    if args.command=='check':return uiemap_config.main([str(args.config)])
    if args.command=='status':return uiemap_status.main([str(args.config)])
    if args.command=='services':return uiemap_services.main([str(args.config)])
    if args.command=='gc':return uiemap_gc.main([str(args.config),'--dimension',args.dimension,
        '--max-files',str(args.max_files),'--max-bytes',str(args.max_bytes)]+(['--apply'] if args.apply else []))
    if args.command=='cave-gc':return uiemap_cave_gc.main([str(args.config),'--dimension',args.dimension,
        '--max-files',str(args.max_files),'--max-bytes',str(args.max_bytes)]+(['--apply'] if args.apply else []))
    if args.command=='update':return uiemap_update.main([str(args.config)]+(['--apply'] if args.apply else [])+(['--gc'] if args.gc else []))
    if args.command=='schedule':return uiemap_schedule.main([str(args.config),'--interval',str(args.interval)]+(['--apply'] if args.apply else [])+(['--gc'] if args.gc else []))
    if args.command=='doctor':return uiemap_doctor.main([str(args.config)])
    if args.command=='catalog':return catalog_package.main(['--source',str(args.source),'--out',str(args.out)]+(['--apply'] if args.apply else []))
    if args.command=='render':
        return uiemap_render.main([str(args.config),'--dimension',args.dimension]+(['--apply'] if args.apply else []))
    if args.command=='collisions':
        return uiemap_collisions.main([str(args.config),'--dimension',args.dimension]+(['--apply'] if args.apply else []))
    return uiemap_serve.main([str(args.config),'--main-dimension',args.main_dimension]+(['--apply'] if args.apply else [])
        +(['--cave-config',str(args.cave_config)] if args.cave_config else [])+(['--collisions'] if args.collisions else []))


if __name__=='__main__':raise SystemExit(main())

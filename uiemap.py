"""UIE Map prototype: check configuration, render a surface, or serve published maps."""
import argparse
import sys
from pathlib import Path
import uiemap_config
import uiemap_render
import uiemap_serve
import uiemap_doctor


def main(argv=None):
    # JSON and Czech titles must also work when Windows redirects a cp1252 console.
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    check=commands.add_parser('check',help='Validate configuration without writing or starting processes')
    check.add_argument('config',type=Path)
    doctor=commands.add_parser('doctor',help='Report missing prerequisites without rendering or starting services')
    doctor.add_argument('config',type=Path)
    render=commands.add_parser('render',help='Plan or run one configured surface dimension (Linux systemd)')
    render.add_argument('config',type=Path);render.add_argument('--dimension',required=True)
    render.add_argument('--apply',action='store_true',help='Actually run render and publication')
    serve=commands.add_parser('serve',help='Plan or start the web viewer for published surface stores')
    serve.add_argument('config',type=Path);serve.add_argument('--main-dimension',required=True)
    serve.add_argument('--apply',action='store_true',help='Write web configuration and start Node in foreground')
    args=parser.parse_args(argv)
    if args.command=='check':return uiemap_config.main([str(args.config)])
    if args.command=='doctor':return uiemap_doctor.main([str(args.config)])
    if args.command=='render':
        return uiemap_render.main([str(args.config),'--dimension',args.dimension]+(['--apply'] if args.apply else []))
    return uiemap_serve.main([str(args.config),'--main-dimension',args.main_dimension]+(['--apply'] if args.apply else []))


if __name__=='__main__':raise SystemExit(main())

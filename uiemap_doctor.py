"""Read-only prerequisite report; does not render, launch services or certify mod compatibility."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uiemap_config
import uiemap_render


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise ValueError('Command unavailable or failed: ' + args[0])
        return result.stdout.strip()
    except subprocess.TimeoutExpired as error:
        raise ValueError('Command timed out: ' + args[0]) from error


def inspect(path):
    checks = []
    def check(name, action, scope='render'):
        try:
            detail = action()
            checks.append(dict(name=name, scope=scope, ok=True, detail=detail))
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
            checks.append(dict(name=name, scope=scope, ok=False, detail=str(error)))
    try:
        config = uiemap_config.load(path)
    except (OSError, ValueError) as error:
        return dict(format='UIEMAP_DOCTOR1', readOnly=True, prerequisitesReady=False,
                    checks=[dict(name='configuration', scope='shared', ok=False, detail=str(error))])
    def readable(catalog, name):
        with (catalog/name).open('rb') as file:
            if not file.read(1):raise ValueError('Empty catalog file: '+name)
        return name
    def compiled(catalog):
        from offline_catalog import Catalog
        if (catalog/'catalog.json').stat().st_size > 64*1024*1024:
            raise ValueError('Catalog exceeds doctor inspection limit (64 MiB)')
        loaded=Catalog.load_compiled(catalog/'catalog.json')
        return dict(identity=loaded.identity, sprites=len(loaded.sprites))
    catalogs={config['catalog']:''}
    for row in config['dimensions']:
        catalogs.setdefault(row['catalog'],'dimension:'+row['key']+':')
    for directory,prefix in catalogs.items():
        catalog=Path(directory)
        for name in ('catalog.json', 'atlas.png', 'atlas-layout.json', 'grass.png', 'foliage.png'):
            check(prefix+'file:'+name, lambda name=name,catalog=catalog: readable(catalog,name))
        check(prefix+'compiled-catalog',lambda catalog=catalog: compiled(catalog))
    def registry():
        import reader
        source=Path(config['world']['path'])/'level.dat'
        with source.open('rb') as file:raw=file.read(16*1024*1024+1)
        if len(raw)>16*1024*1024:raise ValueError('level.dat exceeds doctor inspection limit')
        result=reader.parse_fml_registry(reader.parse_level_dat(raw))
        if not result.blocks:raise ValueError('Missing Forge block registry')
        return dict(blocks=len(result.blocks))
    check('world-registry', registry)
    for row in config['dimensions']:
        def dimension(row=row):
            planned=uiemap_render.plan(path,row['key'])
            return dict(regions=planned['regions'], freeBytes=uiemap_render.free_space(planned))
        check('dimension:'+row['key'],dimension)
    def systemd():
        if sys.platform!='linux':raise ValueError('Configured rendering requires Linux user systemd')
        for name in ('systemd-run','systemctl'):
            if not shutil.which(name):raise ValueError('Missing executable: '+name)
        value=command(['systemctl','--user','show','--property=Version','--value'])
        if not value:raise ValueError('User systemd manager did not return its version')
        return value
    check('user-systemd',systemd)
    def node():
        executable=shutil.which('node')
        if not executable:raise ValueError('Node.js is missing from PATH')
        version=command([executable,'--version'])
        if int(version.lstrip('v').split('.')[0])<18:raise ValueError('Node.js 18 or newer is required')
        return version
    check('node',node,'web')
    return dict(format='UIEMAP_DOCTOR1', readOnly=True,
                prerequisitesReady=all(c['ok'] for c in checks), checks=checks,
                limitations=['Checks prerequisites only; no render or full region/payload audit.',
                             'Does not prove Minecraft version, modpack/model compatibility, port availability or available cgroup controllers.',
                             'A prepared catalog is still required; automatic modpack import is not implemented.'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config',type=Path)
    args=parser.parse_args(argv)
    result=inspect(args.config)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['prerequisitesReady'] else 2


if __name__=='__main__':raise SystemExit(main())

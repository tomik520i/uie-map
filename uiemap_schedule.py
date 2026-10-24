"""Install an opt-in user-systemd timer for sequential configured updates."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uiemap_update


def quote(value):
    if any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError('Control characters in service path')
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'


def plan(config,interval=900,*,cleanup=False):
    if type(interval) is not int or not 60<=interval<=86400:raise ValueError('Interval must be 60..86400 seconds')
    update=uiemap_update.plan(config)
    if cleanup and any(job.get('mode','surface')!='surface' for job in update['jobs']):
        raise ValueError('Scheduled GC currently supports surface stores only')
    name='uiemap-update-'+hashlib.sha256(update['config'].encode()).hexdigest()[:20]
    command=' '.join(quote(value) for value in ([sys.executable,'-B',str(Path(__file__).with_name('uiemap.py').resolve()),
                                                'update',update['config'],'--apply']+(['--gc'] if cleanup else [])))
    service='\n'.join(['[Unit]','Description=UIE Map configured surface update','', '[Service]',
        'Type=oneshot','ExecStart='+command,'KillMode=control-group','KillSignal=SIGINT',
        'TimeoutStartSec=infinity','TimeoutStopSec=90','CPUWeight=10','IOWeight=10','Nice=15',''])
    timer='\n'.join(['[Unit]','Description=UIE Map update schedule','', '[Timer]',
        'OnActiveSec=30','OnUnitInactiveSec='+str(interval),'AccuracySec=1','Unit='+name+'.service',
        '', '[Install]','WantedBy=timers.target',''])
    return dict(format='UIEMAP_SCHEDULE_PLAN1',name=name,config=update['config'],intervalSeconds=interval,
                files={name+'.service':service,name+'.timer':timer},readOnly=True,collectAfterPublication=cleanup,
                notes=['First update starts after 30 seconds; interval is measured from completion.',
                       'One systemd service cannot overlap itself. Manual updates are separate.',
                       'Stopping only the timer prevents future starts; stop the service to interrupt the active pass.',
                       'User systemd must remain available; this command does not enable lingering.'])


def install_files(work,directory):
    directory=Path(directory).absolute()
    if directory.resolve()!=directory:raise ValueError('Linked systemd unit directory is not allowed')
    directory.mkdir(parents=True,exist_ok=True)
    # Check every destination before writing any file. Never replace an unrelated unit.
    for name,body in work['files'].items():
        path=directory/name
        if path.is_symlink() or (path.exists() and path.read_text(encoding='utf-8')!=body):
            raise ValueError('Existing unit differs: '+name)
    created=[]
    try:
        for name,body in work['files'].items():
            path=directory/name
            if not path.exists():
                with path.open('x',encoding='utf-8',newline='\n') as file:
                    created.append(path);file.write(body);file.flush();os.fsync(file.fileno())
    except BaseException:
        for path in created:path.unlink()
        raise
    return directory


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('config',type=Path)
    parser.add_argument('--interval',type=int,default=900);parser.add_argument('--apply',action='store_true')
    parser.add_argument('--gc',action='store_true',help='Enable bounded collection after each published dimension')
    args=parser.parse_args(argv)
    try:
        work=plan(args.config,args.interval,cleanup=args.gc)
        if args.apply:
            if sys.platform!='linux':raise ValueError('Schedule installation requires Linux user systemd')
            subprocess.run(['systemctl','--user','show','--property=Version','--value'],check=True,capture_output=True,timeout=10)
            directory=install_files(work,Path.home()/'.config/systemd/user')
            subprocess.run(['systemctl','--user','daemon-reload'],check=True,timeout=30)
            subprocess.run(['systemctl','--user','enable','--now',work['name']+'.timer'],check=True,timeout=30)
            work.update(readOnly=False,installed=True,directory=str(directory))
    except (OSError,ValueError,subprocess.SubprocessError) as error:parser.exit(2,str(error)+'\n')
    print(json.dumps(work,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())

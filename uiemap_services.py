"""Read actual user-systemd state of the schedule belonging to a configuration path."""
import argparse,json,subprocess,sys
from pathlib import Path
from uiemap_schedule import unit_name

FIELDS=('LoadState','ActiveState','SubState','UnitFileState','Result','MainPID','ExecMainStatus',
        'ExecMainStartTimestamp','LastTriggerUSec','NextElapseUSecRealtime','NextElapseUSecMonotonic')


def inspect(config,runner=None):
    name=unit_name(config);result=dict(format='UIEMAP_SERVICES1',config=str(Path(config).resolve()),
                                    name=name,readOnly=True,units={})
    if sys.platform!='linux':return dict(result,supported=False,reason='Requires Linux user systemd')
    runner=subprocess.run if runner is None else runner
    for suffix in ('timer','service'):
        command=['systemctl','--user','show',name+'.'+suffix,'--property='+','.join(FIELDS)]
        try:
            proc=runner(command,capture_output=True,text=True,timeout=10)
            properties={key:value for line in proc.stdout.splitlines() if '=' in line
                        for key,value in [line.split('=',1)] if key in FIELDS}
            if properties.get('LoadState')=='not-found':
                result['units'][suffix]=dict(installed=False,state='not-installed')
            elif proc.returncode or not properties.get('LoadState'):
                result['units'][suffix]=dict(state='error',error=proc.stderr.strip()[:1000] or 'No systemd state returned')
            else:
                result['units'][suffix]=dict(installed=True,state=properties['ActiveState'] if 'ActiveState' in properties else 'unknown',properties=properties)
        except (OSError,subprocess.TimeoutExpired) as error:
            result['units'][suffix]=dict(state='error',error=str(error)[:1000])
    result.update(supported=True,notes=['Inactive oneshot service can be normal between timer runs.',
        'This checks only units created by schedule for this exact configuration path, not legacy production services.',
        'A missing configuration file does not prevent inspecting its existing unit names.'])
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('config',type=Path)
    args=parser.parse_args(argv);result=inspect(args.config)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 2 if not result.get('supported') or any(row['state'] in ('error','failed') for row in result['units'].values()) else 0


if __name__=='__main__':raise SystemExit(main())

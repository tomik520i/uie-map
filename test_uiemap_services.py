import subprocess,unittest,io,contextlib
from unittest.mock import patch
import uiemap_services as services
from uiemap_schedule import unit_name


class ServicesTests(unittest.TestCase):
    def inspect(self,runner):
        with patch.object(services.sys,'platform','linux'):return services.inspect('missing-config.json',runner)

    def test_missing_units_are_not_failures_or_running(self):
        result=self.inspect(lambda *a,**k:subprocess.CompletedProcess([],1,'LoadState=not-found\n',''))
        self.assertTrue(result['readOnly'])
        self.assertEqual(result['name'],unit_name('missing-config.json'))
        self.assertTrue(all(row['state']=='not-installed' for row in result['units'].values()))

    def test_actual_timer_and_completed_service_are_distinct(self):
        calls=[]
        def run(args,**kwargs):
            calls.append(args)
            body='LoadState=loaded\nActiveState='+('active' if args[3].endswith('.timer') else 'inactive')+'\nResult=success\nUnitFileState=enabled\n'
            return subprocess.CompletedProcess([],0,body,'')
        result=self.inspect(run)
        self.assertEqual(result['units']['timer']['state'],'active');self.assertEqual(result['units']['service']['state'],'inactive')
        self.assertTrue(all(args[:3]==['systemctl','--user','show'] for args in calls))

    def test_unavailable_manager_and_timeout_not_reported_as_not_installed(self):
        result=self.inspect(lambda *a,**k:subprocess.CompletedProcess([],1,'','Failed to connect to bus'))
        self.assertEqual(result['units']['timer']['state'],'error')
        def timeout(*a,**k):raise subprocess.TimeoutExpired('systemctl',10)
        self.assertEqual(self.inspect(timeout)['units']['service']['state'],'error')

    def test_failed_unit_has_nonzero_cli_status(self):
        result=self.inspect(lambda *a,**k:subprocess.CompletedProcess([],0,'LoadState=loaded\nActiveState=failed\nResult=exit-code\n',''))
        with patch.object(services,'inspect',return_value=result),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(services.main(['missing-config.json']),2)


if __name__=='__main__':unittest.main()

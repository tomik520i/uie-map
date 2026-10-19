import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uiemap_doctor as doctor


class DoctorTests(unittest.TestCase):
    def test_invalid_configuration_is_structured_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            result=doctor.inspect(Path(temp)/'missing.json')
            self.assertFalse(result['prerequisitesReady'])
            self.assertEqual(result['checks'][0]['name'],'configuration')

    def test_missing_inputs_all_reported_and_no_output_created(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'world/region').mkdir(parents=True);(root/'catalog').mkdir()
            file=root/'config.json'
            file.write_text(json.dumps(dict(format='UIEMAP_CONFIG1',world=dict(id='test',path='world'),
                catalog='catalog',out='out',dimensions=[dict(key='0',title='World',mode='surface')])))
            before=set(root.rglob('*'))
            run=subprocess.run([sys.executable,'-B',str(Path(doctor.__file__).with_name('uiemap.py')),
                                'doctor',str(file)],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(run.returncode,2,run.stderr)
            result=json.loads(run.stdout)
            failures={c['name'] for c in result['checks'] if not c['ok']}
            self.assertTrue({'compiled-catalog','world-registry','dimension:0','file:atlas.png'}<=failures)
            self.assertEqual(before,set(root.rglob('*')))
            self.assertTrue(result['readOnly'])
            (root/'catalog/catalog.json').write_text('[]')
            broken=doctor.inspect(file)
            self.assertFalse(next(c for c in broken['checks'] if c['name']=='compiled-catalog')['ok'])

    def test_probe_timeout_and_nonzero_fail(self):
        with patch.object(doctor.subprocess,'run',side_effect=subprocess.TimeoutExpired('node',10)):
            with self.assertRaisesRegex(ValueError,'timed out'):doctor.command(['node','--version'])
        with patch.object(doctor.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','')):
            with self.assertRaisesRegex(ValueError,'failed'):doctor.command(['systemctl','--user','show'])


if __name__=='__main__':unittest.main()

import json,subprocess,sys,tempfile,unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_real_cli_defaults_are_read_only_and_bad_commands_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'world/region').mkdir(parents=True);(root/'catalog').mkdir()
            (root/'world/region/r.0.0.mca').write_bytes(b'\0'*8192)
            file=root/'config.json';file.write_text(json.dumps(dict(format='UIEMAP_CONFIG1',
                world=dict(id='test',path='world'),catalog='catalog',out='out',
                dimensions=[dict(key='0',title='Svět',mode='surface')])))
            before=set(root.rglob('*'));script=Path(__file__).with_name('uiemap.py')
            for command in (['check',str(file)],['render',str(file),'--dimension','0']):
                run=subprocess.run([sys.executable,str(script),*command],capture_output=True,text=True,encoding='utf-8')
                self.assertEqual(run.returncode,0,run.stderr);self.assertTrue(json.loads(run.stdout)['readOnly'])
                self.assertEqual(before,set(root.rglob('*')))
            # Serving unpublished data, mistyped flags and unknown commands must not start anything.
            for command in (['serve',str(file),'--main-dimension','0'],['render',str(file),'--dimension','0','--aply'],['unknown']):
                run=subprocess.run([sys.executable,str(script),*command],capture_output=True,text=True,encoding='utf-8')
                self.assertEqual(run.returncode,2,run.stderr);self.assertEqual(before,set(root.rglob('*')))


if __name__=='__main__':unittest.main()

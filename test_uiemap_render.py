import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import uiemap_render as render


class RenderConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        for name in ('world/region','world/DIM42/region','world/TROPICS/region','catalog'):(self.root/name).mkdir(parents=True)
        for name in ('region','DIM42/region','TROPICS/region'):(self.root/'world'/name/'r.0.0.mca').write_bytes(b'\0'*8192)
        self.doc=dict(format='UIEMAP_CONFIG1',world=dict(id='mine',path='world'),catalog='catalog',out='out',
          dimensions=[dict(key='42',title='Další svět',mode='surface')],limits=dict(workers=2,memoryMiB=1024,minFreeGiB=25))
        self.file=self.root/'config.json';self.save()
    def save(self):self.file.write_text(json.dumps(self.doc),encoding='utf-8')
    def test_dry_plan_writes_nothing_and_selects_full_dimension(self):
        before=set(self.root.rglob('*'));p=render.plan(self.file,'42')
        self.assertEqual(before,set(self.root.rglob('*')));self.assertTrue(p['readOnly']);self.assertFalse(p['webChanged'])
        command=p['command'];self.assertNotIn('--max-jobs',command)
        self.assertEqual(command[command.index('--dimension-key')+1],'42')
        self.assertEqual(command[command.index('--world')+1],str(self.root/'world'))
        self.assertEqual(command[command.index('--workers')+1],'2')
    def test_only_explicit_supported_dimensions(self):
        with self.assertRaises(ValueError):render.plan(self.file,'0')
        self.doc['dimensions'][0]['mode']='caves';self.save()
        cave=render.plan(self.file,'42')
        self.assertIn('--caves',cave['command']);self.assertIn('--cave-caps',cave['command'])
        self.assertEqual(cave['binding']['mode'],'caves')
        self.assertEqual(Path(cave['out']).name,'caves')

    def test_selected_catalog_reaches_both_surface_and_cave_renderer(self):
        (self.root/'crystals').mkdir()
        self.doc['dimensions'][0]['catalog']='crystals'
        for mode in ['surface','caves']:
            self.doc['dimensions'][0]['mode']=mode;self.save()
            command=render.plan(self.file,'42')['command']
            self.assertEqual(command[command.index('--catalog-dir')+1],str(self.root/'crystals'))

    def test_cave_success_requires_both_publication_receipts(self):
        self.doc['dimensions'][0]['mode']='caves';self.save();work=render.plan(self.file,'42')
        result=dict(published=True,indexSha256='a'*64)
        with self.assertRaisesRegex(ValueError,'caps'):render.publication_receipt(work,result)
        result['capPublication']=dict(published=False,indexSha256='b'*64)
        with self.assertRaises(ValueError):render.publication_receipt(work,result)
        result['capPublication']['published']=True
        self.assertEqual(render.publication_receipt(work,result)['capIndexSha256'],'b'*64)
    def test_custom_dimension_preserves_path(self):
        self.doc['dimensions']=[dict(key='x:tropics',path='TROPICS',title='Tropy',mode='surface')];self.save()
        p=render.plan(self.file,'x:tropics');self.assertEqual(p['binding']['regionPath'],str(self.root/'world/TROPICS/region'))
    def test_legacy_output_is_not_adopted(self):
        p=render.plan(self.file,'42');out=Path(p['out']);out.mkdir(parents=True);(out/'index.json').write_text('{}')
        with self.assertRaises((OSError,ValueError)):render.plan(self.file,'42')
        self.assertEqual((out/'index.json').read_text(),'{}')
    def test_memory_limit_covers_entire_service_and_no_shell_command(self):
        p=render.plan(self.file,'42');command=render.service_command(p,'uiemap-render-test.service')
        self.assertIn('--property=MemoryMax=1024M',command)
        self.assertIn('--property=KillMode=control-group',command)
        self.assertIn('--worker-config-sha',command);self.assertIn(p['configSha256'],command)
        with self.assertRaises(ValueError):render.service_command(p,'unrelated.service')
    def test_disk_floor(self):
        p=render.plan(self.file,'42')
        with patch.object(render.shutil,'disk_usage',return_value=type('Usage',(),{'free':24*1024**3})()):
            with self.assertRaisesRegex(ValueError,'reserve'):render.free_space(p)
    def test_worker_requires_resource_service(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(ValueError,'systemd'):render.run_worker(render.plan(self.file,'42'))
    @unittest.skipUnless(os.name=='posix','Linux worker')
    def test_failed_preparation_does_not_spin_publisher_or_change_pointer(self):
        import subprocess,hires_publisher
        work=render.plan(self.file,'42');out=render.prepare_output(work)
        pointer=out/'current.json';pointer.write_bytes(b'previous publication')
        for kind in ['error','skipped']:
            state=dict(planComplete=True,failedJobs=0,failedTiles=0,immutablePreparation=dict(state=kind,message='empty geometry',reason='reserve'))
            with patch.dict(os.environ,{'INVOCATION_ID':'test'}),patch.object(render,'free_space'),patch.object(render,'prepare_output',return_value=out),patch.object(render.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(state),'')),patch.object(hires_publisher,'run_once') as publish:
                with self.assertRaisesRegex(ValueError,'Geometry preparation failed'):render.run_worker(work)
                publish.assert_not_called()
            self.assertEqual(pointer.read_bytes(),b'previous publication')
    @unittest.skipUnless(os.name=='posix','POSIX binding locks')
    def test_owned_output_and_repeat_binding(self):
        p=render.plan(self.file,'42');out=render.prepare_output(p)
        self.assertTrue((out/'.uie-hires-bundle-owned').is_file())
        self.assertEqual(render.plan(self.file,'42')['binding'],p['binding'])
        self.assertEqual(render.prepare_output(p),out)
    @unittest.skipUnless(os.name=='posix','POSIX symlink')
    def test_output_ancestor_redirect_is_refused(self):
        p=render.plan(self.file,'42');out=Path(p['out']);out.parent.parent.mkdir()
        out.parent.symlink_to(self.root/'catalog',target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'Linked'):render.plan(self.file,'42')


if __name__=='__main__':unittest.main()

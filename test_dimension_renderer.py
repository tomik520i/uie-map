import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import dimension_binding as binding
import world_hires as wh
import hires_bundle as hb
import hires_publish_tick as tick


class DimensionRendererTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name).resolve()
        self.world=self.base/'world';(self.world/'region').mkdir(parents=True);(self.world/'DIM-1/region').mkdir(parents=True)
        self.out=self.base/'out';self.out.mkdir();(self.out/hb.OWNED_MARKER).write_bytes(b'owned')
        self.doc=binding.make_binding(world=self.world,world_id='demo',dimension='-1',mode='surface')
    def mark(self): (self.out/binding.NAME).write_bytes(binding.canonical(self.doc))
    def test_legacy_default_remains_overworld(self):
        self.assertEqual(wh.resolve_region_source(self.world,self.out),self.world/'region')
        self.assertFalse((self.out/binding.NAME).exists())
    def test_bound_output_cannot_be_used_with_legacy_defaults(self):
        self.mark()
        with self.assertRaisesRegex(wh.HiresError,'explicit'):wh.resolve_region_source(self.world,self.out)
    @unittest.skipUnless(os.name=='posix','POSIX init')
    def test_explicit_dimension_resolves_region_and_refuses_retarget(self):
        result=wh.resolve_region_source(self.world,self.out,world_id='demo',dimension_key='-1')
        self.assertEqual(result,self.world/'DIM-1/region')
        self.assertEqual(binding.read_binding(self.out),self.doc)
        for kwargs in (dict(world_id='demo',dimension_key='0'),dict(world_id='other',dimension_key='-1'),dict(world_id='demo',dimension_key='-1',caves=True)):
            with self.assertRaises(binding.BindingError):wh.resolve_region_source(self.world,self.out,**kwargs)
    def test_incomplete_options_and_custom_path_refused(self):
        for kwargs in (dict(world_id='demo'),dict(dimension_key='-1'),dict(world_id='demo',dimension_key='x:custom')):
            with self.assertRaises((ValueError,wh.HiresError)):wh.resolve_region_source(self.world,self.out,**kwargs)
    def test_direct_publisher_rejects_wrong_context_before_reading_objects(self):
        self.mark()
        for context in (None,dict(world='demo',dimension='0',caves=False),dict(world='demo',dimension='-1',caves=True)):
            with patch.object(hb,'_current_index',side_effect=AssertionError('must refuse before store read')):
                with self.assertRaises(binding.BindingError):hb.publish_index(self.out,{},'a'*64,'b'*64,context=context)
        binding.check_context(self.out,dict(world='demo',dimension='-1',caves=False))
    def test_tick_refuses_namespace_before_staging(self):
        self.mark()
        from types import SimpleNamespace
        with patch.object(tick.shutil,'disk_usage',return_value=SimpleNamespace(free=100*1024**3)),patch.object(tick.hs,'_read_index',return_value=b'{}'),patch.object(tick.hs,'_parse_index',return_value={'caves':False}),patch.object(tick.bf,'step',side_effect=AssertionError('must not stage')):
            with self.assertRaises(binding.BindingError):tick.tick(self.out,None,world='wrong',dimension='-1')
    def test_cli_refuses_input_overlap_before_creating_lock(self):
        target=self.world/'render-output'
        with self.assertRaises(SystemExit) as error:
            wh.main(['--world',str(self.world),'--catalog-dir',str(self.base/'catalog'),'--out',str(target),
                     '--world-id','demo','--dimension-key','0'])
        self.assertEqual(error.exception.code,2);self.assertFalse(target.exists())
    @unittest.skipUnless(os.name=='posix','POSIX init')
    def test_build_reads_shared_registry_and_selected_region(self):
        from types import SimpleNamespace
        import reader,offline_catalog
        catalog=self.base/'catalog';catalog.mkdir()
        (catalog/'atlas.png').write_bytes(b'atlas');(catalog/'atlas-layout.json').write_bytes(b'{}')
        (self.world/'level.dat').write_bytes(b'shared-registry')
        with patch.object(reader,'parse_level_dat',return_value={}) as level,patch.object(reader,'parse_fml_registry',return_value=SimpleNamespace(blocks={})),patch.object(offline_catalog.Catalog,'load_compiled',return_value=SimpleNamespace(identity='catalog')),patch.object(wh,'plan_jobs',return_value=[]) as jobs:
            result=wh.build(self.world,catalog,self.out,1,world_id='demo',dimension_key='-1')
            level.assert_called_once_with(b'shared-registry');jobs.assert_called_once_with(self.world/'DIM-1/region',(0,0))
        self.assertTrue(result['planComplete']);self.assertEqual(result['failedJobs'],0)


@unittest.skipUnless(os.name=='posix','POSIX publisher')
class DimensionPublicationTests(unittest.TestCase):
    def test_full_publisher_keeps_dimension_and_refuses_default_namespace(self):
        import json
        import test_hires_publish_tick as fixture
        helper=fixture.TickTests('test_tick_converges_preparation_and_verification_then_publishes_context')
        helper.setUp();self.addCleanup(helper.doCleanups)
        world=helper.out.parent/'dimension-world';(world/'DIM-1/region').mkdir(parents=True)
        doc=binding.make_binding(world=world,world_id='demo',dimension='-1',mode='surface')
        (helper.out/binding.NAME).write_bytes(binding.canonical(doc));helper.receipt()
        result=tick.tick(helper.out,helper.session,world='demo',dimension='-1')
        self.assertTrue(result['published'])
        pointer=(helper.out/'current.json').read_bytes()
        index=json.loads((helper.out/'indexes'/(result['indexSha256']+'.json')).read_bytes())
        self.assertEqual(index['context']['world'],'demo');self.assertEqual(index['context']['dimension'],'-1')
        with self.assertRaises(binding.BindingError):helper.tick()
        self.assertEqual((helper.out/'current.json').read_bytes(),pointer)


if __name__=='__main__':unittest.main()

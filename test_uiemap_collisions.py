import json,unittest
from pathlib import Path
from unittest.mock import patch
import test_uiemap_serve as fixtures
import uiemap_collisions as collisions
import dimension_binding as binding


class CollisionConfigTests(unittest.TestCase):
    index=fixtures.WebConfigTests.index
    def setUp(self):
        fixtures.WebConfigTests.setUp(self)
        for name in ['world/region','world/DIM42/region']:(self.root/name/'r.0.0.mca').write_bytes(bytes(8192))

    def test_plan_uses_selected_regions_and_root_registry_without_writes(self):
        before=set(self.root.rglob('*'));work=collisions.plan(self.file,'42')
        self.assertEqual(before,set(self.root.rglob('*')))
        self.assertEqual(work['worldPath'],str(self.root/'world'))
        self.assertEqual(work['regionWorld'],str(self.root/'world/DIM42'))
        self.assertEqual(work['binding']['mode'],'collisions')
        self.assertTrue(work['out'].endswith('collisions'))
        self.assertEqual(work['surface'],str(self.stores['42']))

    def test_foreign_collision_binding_and_missing_surface_refused(self):
        work=collisions.plan(self.file,'42');out=Path(work['out']);out.mkdir(parents=True)
        other=dict(work['binding'],dimension='0');(out/binding.NAME).write_bytes(binding.canonical(other))
        with self.assertRaises(ValueError):collisions.plan(self.file,'42')
        (self.stores['0']/'current.json').unlink()
        with self.assertRaisesRegex(ValueError,'published'):collisions.plan(self.file,'0')

    def test_service_uses_collision_worker_and_memory_limit(self):
        work=collisions.plan(self.file,'42');cmd=collisions.service_command(work,'uiemap-render-test.service')
        self.assertIn(str(Path(collisions.__file__).resolve()),cmd)
        self.assertIn('--property=MemoryMax=2048M',cmd)
        self.assertIn(work['configSha256'],cmd)

    def test_collision_plan_uses_same_dimension_catalog_as_surface(self):
        (self.root/'crystals').mkdir()
        doc=json.loads(self.file.read_text())
        for row in doc['dimensions']:
            if row['key']=='42':row['catalog']='crystals'
        self.file.write_text(json.dumps(doc))
        self.assertEqual(collisions.plan(self.file,'42')['catalog'],str(self.root/'crystals'))
        self.assertEqual(collisions.plan(self.file,'0')['catalog'],str(self.root/'catalog'))

    def test_dimension_toolkit_fresh_preserves_registry_and_index_identity(self):
        import collision_region_probe as probe
        import collision_export as export
        work=collisions.plan(self.file,'42')
        with patch.object(probe.Toolkit,'__init__',return_value=None) as init,patch.object(export,'read_registry',return_value={1:'stone'}) as registry,patch.object(probe,'read_visible',return_value={}) as visible:
            for tools in [collisions.make_tools(work),collisions.make_tools(work).fresh()]:
                self.assertEqual(tools.load_registry(Path(work['regionWorld'])),{1:'stone'})
                registry.assert_called_with(self.root/'world')
            init.call_args.kwargs['read_index'](Path(work['surface']))
            visible.assert_called_once_with(Path(work['surface']),world='mine',dimension='42')


if __name__=='__main__':unittest.main()

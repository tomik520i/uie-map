import hashlib,json,os,unittest
from pathlib import Path
from unittest.mock import patch
import test_modpack_rules as fixture
import modpack_atlas,explicit_catalog as ec,world_hires as wh,atlas_identity
from test_reader import SectionBuilder,chunk_nbt,record,build_region


class ExplicitHiresTests(unittest.TestCase):
    setUp=fixture.RuleTests.setUp
    jar=fixture.RuleTests.jar
    inspect=fixture.RuleTests.inspect
    def prepare(self):
        from test_reader import level_dat
        (self.world/'level.dat').write_bytes(level_dat([('\x01minecraft:air',0),('\x01sample:stone',51),('\x01other:stone',52)]))
        self.inspect();modpack_atlas.build(self.mods,self.world,self.path,self.root/'atlas',True)
        ec.build(self.root/'atlas',self.root/'catalog',True)
        (self.world/'region').mkdir()
        section=SectionBuilder(0);section.set(1,5,1,51,0);section.set(3,5,1,51,1)
        (self.world/'region/r.0.0.mca').write_bytes(build_region({(0,0):record(chunk_nbt(0,0,[section]))}))
    def test_layout_snapshot_validates_and_rejects_changed_atlas(self):
        self.prepare();cat=ec.ExplicitCatalog.load(self.root/'catalog',{0:'minecraft:air',51:'sample:stone',52:'other:stone'})
        assets=cat.atlas_assets(self.root/'catalog');folder=self.root/'assets';folder.mkdir()
        for name,raw in assets.items():(folder/name).write_bytes(raw)
        layout,digest=atlas_identity.load_layout(folder/'atlas.png',folder/'atlas-layout.json')
        self.assertEqual(digest,cat.document['atlasSha256']);self.assertTrue(layout['reusable'])
        (self.root/'catalog/atlas.png').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):cat.atlas_assets(self.root/'catalog')
    def test_registry_failure_preserves_existing_output_atlas(self):
        self.prepare();out=self.root/'out';out.mkdir();(out/'atlas.png').write_bytes(b'previous')
        from test_reader import level_dat
        (self.world/'level.dat').write_bytes(level_dat([('\x01wrong:block',51)]))
        with self.assertRaisesRegex(ValueError,'different world'):wh.build(self.world,self.root/'catalog',out,1)
        self.assertEqual((out/'atlas.png').read_bytes(),b'previous');self.assertFalse((out/'tiles').exists())
    def test_configured_doctor_accepts_explicit_catalog_without_legacy_files(self):
        self.prepare()
        import uiemap_doctor as doctor,uiemap_render
        config=self.root/'config.json'
        config.write_text(json.dumps(dict(format='UIEMAP_CONFIG1',world=dict(id='fixture',path='world'),
            catalog='catalog',out='configured',dimensions=[dict(key='0',title='Fixture',mode='surface')])))
        result=doctor.inspect(config)
        check=next(c for c in result['checks'] if c['name']=='explicit-catalog')
        self.assertTrue(check['ok'],check);self.assertFalse(check['detail']['modelSemanticsVerified'])
        self.assertFalse(any(c['name']=='file:grass.png' for c in result['checks']))
        planned=uiemap_render.plan(config,'0');self.assertIn(str(self.root/'catalog'),planned['command'])
        self.assertFalse((self.root/'configured').exists())
    @unittest.skipUnless(os.name=='posix','POSIX publisher and process pool')
    def test_surface_and_caves_real_pool_publish_exact_atlas_and_reuse(self):
        self.prepare()
        import hires_bundle as hb,hires_publisher as publisher
        from hires_publish_session import PublishSession
        for caves in (False,True):
            with self.subTest(caves=caves):
                out=self.root/('caves' if caves else 'surface');out.mkdir();(out/hb.OWNED_MARKER).write_text('owned')
                kwargs=dict(caves=caves,cave_caps=caves,immutable_prepare=True,world_id='fixture',dimension_key='0')
                result=wh.build(self.world,self.root/'catalog',out,1,**kwargs)
                self.assertTrue(result['planComplete'],result);self.assertEqual(result['failedTiles'],0)
                job=json.loads((out/'jobs/0_0.json').read_bytes())
                self.assertEqual(job['fallback'],1);self.assertFalse(job.get('diagnosticErrors'))
                index=json.loads((out/'index.json').read_bytes());self.assertEqual(len(index['tiles']),1)
                self.assertEqual(index['atlasSha256'],hashlib.sha256((self.root/'catalog/atlas.png').read_bytes()).hexdigest())
                session=PublishSession()
                for _ in range(100):
                    published=publisher.run_once(out,session,world='fixture',dimension='0',cave_pairs=caves)
                    if published.get('published'):break
                self.assertTrue(published.get('published'),published)
                if caves:self.assertTrue(published['capPublication']['published'])
                again=wh.build(self.world,self.root/'catalog',out,1,**kwargs)
                self.assertEqual(again['skippedUnchanged'],1);self.assertEqual(again['total'],0)

if __name__=='__main__':unittest.main()

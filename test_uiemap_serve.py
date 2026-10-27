import hashlib,json,tempfile,unittest
from pathlib import Path
import dimension_binding as binding
import uiemap_config as config
import uiemap_serve as serve


class WebConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        for name in ('world/region','world/DIM42/region','catalog'):(self.root/name).mkdir(parents=True)
        self.doc=dict(format='UIEMAP_CONFIG1',world=dict(id='mine',path='world'),catalog='catalog',out='out',
          dimensions=[dict(key=k,title='Svět '+k,mode='surface') for k in ('0','42')])
        self.file=self.root/'config.json';self.file.write_text(json.dumps(self.doc))
        self.stores={}
        for row in config.load(self.file)['dimensions']:
            store=Path(row['outputs']['surface']);(store/'indexes').mkdir(parents=True);self.stores[row['key']]=store
            expected=binding.make_binding(world=self.root/'world',world_id='mine',dimension=row['key'],mode='surface')
            (store/binding.NAME).write_bytes(binding.canonical(expected))
            self.index(store,row['key'])
    def index(self,store,dimension):
        doc=dict(context=dict(world='mine',dimension=dimension,caves=False),tiles={'-1,0':'a'*64,'0,1':'b'*64})
        raw=json.dumps(doc).encode();sha=hashlib.sha256(raw).hexdigest()
        (store/'indexes'/(sha+'.json')).write_bytes(raw);(store/'current.json').write_text(json.dumps(dict(indexSha256=sha)))
    def test_preview_no_writes_and_isolated_menu(self):
        before=set(self.root.rglob('*'));plan=serve.snapshot(self.file,'0')
        self.assertEqual(before,set(self.root.rglob('*')));self.assertTrue(plan['readOnly'])
        self.assertEqual(plan['environment']['UIE_HIRES_STORE'],str(self.stores['0']))
        menu=plan['files']['dimensions.json'];self.assertEqual(len(menu['views']),1)
        self.assertEqual(menu['views'][0]['hiresStore'],str(self.stores['42']))
        self.assertNotIn('UIE_GENERATION_STORE',plan['environment'])
        view=next(v for v in plan['files'].values() if v.get('dimension')=='42')
        self.assertEqual(view['bounds'],dict(min=[-64,0,0],max=[64,256,128]))
    def test_snapshot_repeat_is_immutable_and_tampering_refused(self):
        plan=serve.snapshot(self.file,'0');stage=serve.write_snapshot(plan)
        before={p.name:p.read_bytes() for p in stage.iterdir()}
        self.assertEqual(serve.write_snapshot(plan),stage)
        self.assertEqual(before,{p.name:p.read_bytes() for p in stage.iterdir()})
        (stage/'dimensions.json').write_text('{}')
        with self.assertRaises(ValueError):serve.write_snapshot(plan)
    def test_world_identity_and_hash_mismatch_refused(self):
        self.index(self.stores['42'],'0')
        with self.assertRaisesRegex(ValueError,'different'):serve.snapshot(self.file,'0')
        self.index(self.stores['42'],'42')
        pointer=json.loads((self.stores['42']/'current.json').read_text())
        (self.stores['42']/'indexes'/(pointer['indexSha256']+'.json')).write_text('{}')
        with self.assertRaisesRegex(ValueError,'hash'):serve.snapshot(self.file,'0')
    def test_unknown_main_and_unpublished_dimension_refused(self):
        with self.assertRaisesRegex(ValueError,'Main'):serve.snapshot(self.file,'99')
        (self.stores['42']/'current.json').unlink()
        with self.assertRaises(FileNotFoundError):serve.snapshot(self.file,'0')
    def test_main_can_be_nonzero_and_title_changes_new_snapshot(self):
        first=serve.snapshot(self.file,'42')
        self.assertEqual(first['environment']['UIE_HIRES_STORE'],str(self.stores['42']))
        self.doc['dimensions'][1]['title']='Jiný název';self.file.write_text(json.dumps(self.doc))
        self.assertNotEqual(first['directory'],serve.snapshot(self.file,'42')['directory'])

    def cave_fixture(self):
        doc=json.loads(json.dumps(self.doc));doc['dimensions']=doc['dimensions'][1:]
        doc['dimensions'][0]['mode']='caves'
        file=self.root/'caves.json';file.write_text(json.dumps(doc))
        row=config.load(file)['dimensions'][0];store=Path(row['outputs']['caves'])
        (store/'indexes').mkdir(parents=True)
        (store/binding.NAME).write_bytes(binding.canonical(binding.make_binding(world=self.root/'world',world_id='mine',dimension='42',mode='caves')))
        index=dict(context=dict(world='mine',dimension='42',caves=True),tiles={'0,0':'a'*64},rendererIdentity='b'*64,atlasSha256='c'*64)
        raw=json.dumps(index).encode();sha=hashlib.sha256(raw).hexdigest()
        (store/'indexes'/(sha+'.json')).write_bytes(raw);(store/'current.json').write_text(json.dumps(dict(indexSha256=sha)))
        cap=dict(format='UIE_CAVE_CAP_INDEX1',geometryIndexSha256=sha,rendererIdentity='b'*64,atlasSha256='c'*64,capIdentity='d'*64,pairs={'0,0':'e'*64})
        raw=json.dumps(cap,sort_keys=True,separators=(',',':')).encode();sha=hashlib.sha256(raw).hexdigest()
        (store/'cave-data/indexes').mkdir(parents=True)
        (store/'cave-data/indexes'/(sha+'.json')).write_bytes(raw)
        (store/'cave-data/current.json').write_text(json.dumps(dict(format='UIE_CAVE_CAP_POINTER1',indexSha256=sha),sort_keys=True,separators=(',',':')))
        return file,store

    def test_caves_attach_to_main_or_child_without_inheritance(self):
        file,store=self.cave_fixture();before=set(self.root.rglob('*'))
        plan=serve.snapshot(self.file,'0',file)
        self.assertEqual(before,set(self.root.rglob('*')))
        self.assertNotIn('UIE_CAVE_PAIR_STORE',plan['environment'])
        child=plan['files']['dimensions.json']['views'][0]
        self.assertEqual(child['cavePairStore'],str(store/'cave-data'))
        self.assertEqual(plan['files'][Path(child['viewFile']).name]['caves'],True)
        main=serve.snapshot(self.file,'42',file)
        self.assertEqual(main['environment']['UIE_CAVE_GEOMETRY_STORE'],str(store))
        self.assertNotEqual(main['directory'],serve.snapshot(self.file,'42')['directory'])

    def test_missing_caps_or_wrong_world_refuse_web_plan(self):
        file,store=self.cave_fixture()
        (store/'cave-data/current.json').unlink()
        with self.assertRaisesRegex(ValueError,'published together'):serve.snapshot(self.file,'42',file)
        doc=json.loads(file.read_text());doc['world']['id']='other';file.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError,'different world'):serve.snapshot(self.file,'42',file)

    def test_new_geometry_cannot_reuse_old_cap_index(self):
        file,store=self.cave_fixture()
        pointer=json.loads((store/'current.json').read_text())
        doc=json.loads((store/'indexes'/(pointer['indexSha256']+'.json')).read_text())
        doc['tiles']['0,0']='f'*64
        raw=json.dumps(doc).encode();sha=hashlib.sha256(raw).hexdigest()
        (store/'indexes'/(sha+'.json')).write_bytes(raw)
        (store/'current.json').write_text(json.dumps(dict(indexSha256=sha)))
        with self.assertRaisesRegex(ValueError,'published together'):serve.snapshot(self.file,'42',file)


if __name__=='__main__':unittest.main()

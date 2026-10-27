import json,hashlib,unittest
from pathlib import Path
import test_uiemap_serve as fixtures
import uiemap_config,uiemap_serve as serve,dimension_binding as binding
import collision_bundle as cb

class CollisionWebTests(unittest.TestCase):
    index=fixtures.WebConfigTests.index
    def setUp(self):
        fixtures.WebConfigTests.setUp(self)
        self.doc['dimensions']=self.doc['dimensions'][1:];self.file.write_text(json.dumps(self.doc))
        row=uiemap_config.load(self.file)['dimensions'][0]
        surface=self.stores['42'];pointer=json.loads((surface/'current.json').read_text())
        geom=json.loads((surface/'indexes'/(pointer['indexSha256']+'.json')).read_text());geom['atlasSha256']='c'*64
        raw=json.dumps(geom).encode();sha=hashlib.sha256(raw).hexdigest()
        (surface/'indexes'/(sha+'.json')).write_bytes(raw);(surface/'current.json').write_text(json.dumps(dict(indexSha256=sha)))
        out=Path(row['outputs']['collisions']);out.mkdir()
        (out/binding.NAME).write_bytes(binding.canonical(binding.make_binding(world=self.root/'world',world_id='mine',dimension='42',mode='collisions')))
        self.store=out/'store';(self.store/'pairs').mkdir(parents=True);(self.store/'indexes').mkdir()
        pairs={}
        for key,sha in geom['tiles'].items():
            x,z=map(int,key.split(','));pair=dict(format=cb.PAIR_FORMAT,x=x*64,z=z*64,atlasSha256='c'*64,geometrySha256=sha,blocksSha256='d'*64)
            raw=cb.canonical(pair);digest=hashlib.sha256(raw).hexdigest();(self.store/'pairs'/(digest+'.json')).write_bytes(raw);pairs[key]=digest
        raw=cb.canonical(dict(format=cb.INDEX_FORMAT,tileSize=64,pairs=pairs));sha=hashlib.sha256(raw).hexdigest()
        (self.store/'indexes'/(sha+'.json')).write_bytes(raw)
        (self.store/'current.json').write_bytes(cb.canonical(dict(format=cb.POINTER_FORMAT,indexSha256=sha)))

    def test_explicit_opt_in_reads_metadata_without_writes(self):
        before=set(self.root.rglob('*'));plan=serve.snapshot(self.file,'42',include_collisions=True)
        self.assertEqual(before,set(self.root.rglob('*')))
        self.assertEqual(plan['environment']['UIE_COLLISION_STORE'],str(self.store))
        view=next(v for v in plan['files'].values() if v.get('dimension')=='42')
        self.assertEqual(view['format'],'UIE_MAP_VIEW3');self.assertTrue(view['collisions']);self.assertFalse(view['caves'])
        self.assertNotIn('UIE_COLLISION_STORE',serve.snapshot(self.file,'42')['environment'])

    def test_stale_surface_refused(self):
        self.index(self.stores['42'],'42') # drops atlas identity: no longer matching collision export
        with self.assertRaisesRegex(ValueError,'differs'):serve.snapshot(self.file,'42',include_collisions=True)

    def test_corrupt_pair_refused(self):
        # Keep surface valid: otherwise another pair may reject its atlas first,
        # depending on directory iteration order on this filesystem.
        next((self.store/'pairs').glob('*.json')).write_text('{}')
        with self.assertRaisesRegex(ValueError,'hash'):serve.snapshot(self.file,'42',include_collisions=True)

if __name__=='__main__':unittest.main()

import contextlib,hashlib,io,json,unittest
from unittest.mock import patch
import explicit_catalog as ec
import modpack_atlas,png_io
import test_modpack_rules as fixtures
from test_offline_renderer import volume
from offline_mesher import Mesher


class ExplicitTests(unittest.TestCase):
    setUp=fixtures.RuleTests.setUp
    jar=fixtures.RuleTests.jar
    inspect=fixtures.RuleTests.inspect
    registry={51:'sample:stone',52:'other:stone'}
    def make(self):
        self.inspect();modpack_atlas.build(self.mods,self.world,self.path,self.root/'atlas',True)
        report=ec.build(self.root/'atlas',self.root/'catalog',True)
        return ec.ExplicitCatalog.load(self.root/'catalog',self.registry),report
    def test_exact_metadata_and_assumed_tier(self):
        cat,report=self.make()
        for meta in (0,2):
            m=cat.model('sample:stone',meta);self.assertEqual((m.kind,m.tier,m.opacity),('cube','assumed','solid'))
        for name,meta in [('sample:stone',1),('other:stone',0),('unknown:block',0)]:
            self.assertEqual(cat.model(name,meta).kind,'fallback')
        self.assertEqual(cat.model('minecraft:air',0).kind,'none')
        self.assertTrue(report['mesherReady']);self.assertFalse(report['batchRendererReady'])
        self.assertFalse(report['modelSemanticsVerified'])
    def test_real_mesher_reports_missing_metadata_and_uses_correct_uv(self):
        cat,_=self.make();vol=volume({(1,5,1):(51,0),(3,5,1):(51,1),(5,5,1):(52,0)})
        vol.registry=dict(self.registry)
        mesh=Mesher(cat,None,caves=True).mesh_batch(vol)
        self.assertEqual(mesh['stats']['renderedBlocks'],3)
        self.assertEqual(mesh['stats']['assumedBlocks'][('sample:stone',0)],1)
        self.assertEqual(mesh['stats']['fallbackBlocks'][('sample:stone',1)],1)
        self.assertEqual(mesh['stats']['fallbackBlocks'][('other:stone',0)],1)
        self.assertEqual(len(mesh['chunks'][(0,0)]['indices']),3*6*6)
        self.assertIn(round(cat.missing_uv[1],7),[round(v,7) for v in mesh['chunks'][(0,0)]['uvs']])
    def test_cutout_keeps_neighbour_faces_and_fallback_is_opaque(self):
        self.doc['blocks'].append(dict(self.doc['blocks'][0],name='other:stone',metadata=[0],opacity='cutout'))
        cat,_=self.make();vol=volume({(1,5,1):(51,0),(2,5,1):(52,0)});vol.registry=dict(self.registry)
        mesh=Mesher(cat,None,caves=True).mesh_batch(vol)
        # Solid face behind cutout remains; cutout face against solid can be culled.
        self.assertGreaterEqual(len(mesh['chunks'][(0,0)]['indices']),11*6)
        w,h,_,pixels=png_io.decode((self.root/'catalog'/'atlas.png').read_bytes())
        u0,v0,u1,v1=cat.missing_uv
        for y in range(round(v0*h),round(v1*h)):
            for x in range(round(u0*w),round(u1*w)):self.assertEqual(pixels[(y*w+x)*4+3],255)
    def test_all_transparent_sources_still_have_visible_fallback(self):
        raw=png_io.encode(2,2,4,b'\0'*16);self.jar('sample.jar',raw)
        self.doc['textures']['stone']['sha256']=hashlib.sha256(raw).hexdigest();self.doc['blocks'][0]['opacity']='cutout'
        cat,_=self.make();self.assertEqual(cat.model('other:stone',0).opacity,'solid')
        w,h,_,rgba=png_io.decode((self.root/'catalog'/'atlas.png').read_bytes())
        x=round(cat.missing_uv[0]*w);y=round(cat.missing_uv[1]*h)
        self.assertEqual(bytes(rgba[(y*w+x)*4:(y*w+x+1)*4]),b'\xff'*4)
    def test_registry_mismatch_and_modified_catalog_or_atlas_rejected(self):
        self.make();root=self.root/'catalog'
        with self.assertRaisesRegex(ValueError,'different world'):ec.ExplicitCatalog.load(root,{53:'sample:stone'})
        p=root/'model-catalog.json';raw=p.read_bytes();doc=json.loads(raw);doc['models'][0]['metadata']=1;p.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError,'identity'):ec.ExplicitCatalog.load(root,self.registry)
        p.write_bytes(raw);(root/'atlas.png').write_bytes(png_io.encode(1,1,4,b'\xff'*4))
        with self.assertRaisesRegex(ValueError,'atlas mismatch'):ec.ExplicitCatalog.load(root,self.registry)
    def test_mismatched_input_pixel_hash_rejected(self):
        self.inspect();modpack_atlas.build(self.mods,self.world,self.path,self.root/'atlas',True)
        p=self.root/'atlas'/'atlas.json';d=json.loads(p.read_text());d['sprites']['stone']['rgbaSha256']='0'*64;p.write_text(json.dumps(d))
        with self.assertRaisesRegex(ValueError,'pixels mismatch'):ec.build(self.root/'atlas',self.root/'catalog',True)
        self.assertFalse((self.root/'catalog').exists())
    def test_rehashed_transparent_fallback_rejected(self):
        self.make();root=self.root/'catalog';p=root/'model-catalog.json';d=json.loads(p.read_text())
        w,h,c,rgba=png_io.decode((root/'atlas.png').read_bytes());rgba=bytearray(rgba)
        x=round(d['missing'][0]*w);y=round(d['missing'][1]*h);rgba[(y*w+x)*4+3]=0
        raw=png_io.encode(w,h,c,rgba);(root/'atlas.png').write_bytes(raw)
        d['atlasSha256']=ec.sha(raw);d.pop('identity');d['identity']=ec.sha(ec.canonical(d));p.write_bytes(ec.canonical(d))
        with self.assertRaisesRegex(ValueError,'Fallback must be opaque'):ec.ExplicitCatalog.load(root,self.registry)
    def test_preview_no_overwrite_and_fsync_rollback(self):
        self.inspect();modpack_atlas.build(self.mods,self.world,self.path,self.root/'atlas',True)
        out=self.root/'catalog';preview=ec.build(self.root/'atlas',out)
        self.assertTrue(preview['readOnly']);self.assertFalse(out.exists())
        with patch.object(ec.os,'fsync',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):ec.build(self.root/'atlas',out,True)
        self.assertFalse(list(self.root.glob('.explicit-catalog-*')));self.assertFalse((self.root/'catalog.catalog-lock').exists())
        actual=ec.build(self.root/'atlas',out,True);self.assertEqual(preview['identity'],actual['identity'])
        with self.assertRaises(ValueError):ec.build(self.root/'atlas',out,True)
    def test_unified_cli_preview(self):
        import uiemap
        self.inspect();modpack_atlas.build(self.mods,self.world,self.path,self.root/'atlas',True)
        out=io.StringIO()
        with contextlib.redirect_stdout(out):code=uiemap.main(['modpack-catalog','--source',str(self.root/'atlas'),'--out',str(self.root/'catalog')])
        self.assertEqual(code,0);self.assertFalse((self.root/'catalog').exists())


if __name__=='__main__':unittest.main()

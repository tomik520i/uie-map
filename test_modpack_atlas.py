import contextlib,hashlib,io,json,unittest
from unittest.mock import patch
import modpack_atlas as atlas
import png_io
import test_modpack_rules as rules_tests


class AtlasTests(unittest.TestCase):
    setUp=rules_tests.RuleTests.setUp
    jar=rules_tests.RuleTests.jar
    inspect=rules_tests.RuleTests.inspect
    def build(self,apply=True):
        self.inspect()
        return atlas.build(self.mods,self.world,self.path,self.root/'out',apply)
    def test_roundtrip_png_layout_gutters_and_no_overwrite(self):
        report=self.build();out=self.root/'out';raw=(out/'atlas.png').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),report['atlasSha256'])
        w,h,c,pixels=png_io.decode(raw);layout=json.loads((out/'atlas.json').read_text());sprite=layout['sprites']['stone']
        self.assertEqual(sprite['uv'],[1/w,1/h,3/w,3/h]);self.assertEqual((w,h,c),(4,4,4))
        self.assertEqual(bytes(pixels),b'\xff'*64)
        self.assertFalse(report['catalogReady']);self.assertEqual(len(list(out.iterdir())),4)
        with self.assertRaisesRegex(ValueError,'already exists'):self.build()
    def test_preview_writes_nothing_and_deterministic_atlas(self):
        report=self.build(False);self.assertTrue(report['readOnly']);self.assertFalse((self.root/'out').exists())
        self.assertEqual(report['atlasSha256'],self.build()['atlasSha256'])
    def test_transparent_solid_refused_cutout_preserves_alpha(self):
        raw=png_io.encode(2,2,4,bytes([200,100,50,128])*4);self.jar('sample.jar',raw)
        self.doc['textures']['stone']['sha256']=hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(ValueError,'Solid rule'):self.build()
        self.doc['blocks'][0]['opacity']='cutout';self.build()
        _,_,_,rgba=png_io.decode((self.root/'out'/'atlas.png').read_bytes())
        self.assertEqual(bytes(rgba),bytes([200,100,50,128])*16)
    def test_output_source_is_protected(self):
        self.inspect()
        for out in [self.mods/'new',self.world/'new']:
            with self.assertRaisesRegex(ValueError,'outside'):atlas.build(self.mods,self.world,self.path,out,True)
            self.assertFalse(out.exists())
    def test_changed_png_rejected_between_validation_and_read(self):
        original=atlas.modpack_rules.inspect
        def changed(*args):
            result=original(*args);self.jar('sample.jar',png_io.encode(2,2,4,b'\0'*16));return result
        self.inspect()
        with patch.object(atlas.modpack_rules,'inspect',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'changed after'):atlas.build(self.mods,self.world,self.path,self.root/'out',True)
        self.assertFalse((self.root/'out').exists())
    def test_failure_cleans_own_stage_and_lock(self):
        self.inspect()
        with patch.object(atlas.os,'fsync',side_effect=OSError('disk error')):
            with self.assertRaises(OSError):atlas.build(self.mods,self.world,self.path,self.root/'out',True)
        self.assertFalse((self.root/'out').exists());self.assertFalse((self.root/'out.atlas-lock').exists())
        self.assertFalse(list(self.root.glob('.rule-atlas-*')))
    def test_atlas_budget_before_allocating(self):
        with patch.object(atlas,'MAX_ATLAS_SIDE',1):
            with self.assertRaisesRegex(ValueError,'side limit'):self.build()
        with patch.object(atlas,'MAX_PIXELS',1):
            with self.assertRaisesRegex(ValueError,'pixels'):self.build()
    def test_unified_cli_preview(self):
        import uiemap
        self.inspect();out=io.StringIO()
        with contextlib.redirect_stdout(out):
            code=uiemap.main(['modpack-atlas','--mods',str(self.mods),'--world',str(self.world),'--rules',str(self.path),'--out',str(self.root/'out')])
        self.assertEqual(code,0);self.assertTrue(json.loads(out.getvalue())['readOnly']);self.assertFalse((self.root/'out').exists())

    def test_multiple_sizes_keep_exact_pixels_and_separate_gutters(self):
        raw=png_io.encode(1,1,4,bytes([0,255,0,255]));self.jar('green.jar',raw)
        self.doc['textures']['zgreen']=dict(archive='green.jar',entry=self.entry,sha256=hashlib.sha256(raw).hexdigest())
        self.doc['blocks'][0]['faces'][1]='zgreen';self.build()
        w,h,_,pixels=png_io.decode((self.root/'out'/'atlas.png').read_bytes())
        self.assertEqual((w,h),(8,4))
        for y in range(3):
            for x in range(4,7):self.assertEqual(bytes(pixels[(y*w+x)*4:(y*w+x+1)*4]),bytes([0,255,0,255]))
        self.assertEqual(bytes(pixels[7*4:8*4]),bytes(4))


if __name__=='__main__':unittest.main()

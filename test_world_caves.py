import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import world_hires as wh

class CaveModeTests(unittest.TestCase):
    def test_surface_identity_unchanged_caves_separate(self):
        expected=hashlib.sha256(b'catalog'+hashlib.sha256(b'code').digest()).hexdigest()
        self.assertEqual(wh.geometry_identity('catalog',b'code'),expected)
        self.assertNotEqual(wh.geometry_identity('catalog',b'code',True),expected)
        self.assertNotEqual(wh.geometry_identity('catalog',b'code',True,True),wh.geometry_identity('catalog',b'code',True))
        with self.assertRaises(wh.HiresError):wh.geometry_identity('catalog',b'code',False,True)

    def test_caps_need_caves_before_output_is_created(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);out=root/'out'
            with self.assertRaises(wh.HiresError):wh.build(root/'missing',root/'missing',out,1,cave_caps=True)
            self.assertFalse(out.exists())

    def test_pair_failure_keeps_previous_alias_and_no_record_reference(self):
        from types import SimpleNamespace
        from test_world_hires import quad
        marker=object();catalog=object()
        mesher=SimpleNamespace(catalog=catalog,mesh_batch=lambda vol:{'chunks':{(0,0):quad(1,30,1)},'stats':{'fallbackBlocks':{}}})
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);(out/'tiles').mkdir();alias=out/'tiles/0_0.uih';alias.write_bytes(b'previous')
            area,core=wh.tile_area(0,0)
            record={'tiles':{},'chunks':0,'quads':0,'bytes':0,'fallback':0,'errors':[]}
            with patch.object(wh,'read_chunks',return_value={(0,0):object()}), \
                 patch.object(wh,'_batch',return_value=marker), \
                 patch.dict(wh._W,{'mesher':mesher,'cap_binding':{'atlas_sha':'a'*64,'renderer_identity':'b'*64}}), \
                 patch('cave_cap_export.build_cap_tile',return_value=(b'caps',{})) as export, \
                 patch('cave_pair_store.store',side_effect=OSError('cap disk full')):
                wh._render_rows(record,{},area,core,out,None,lambda batch:batch)
            export.assert_called_once_with(marker,catalog)
            self.assertEqual(alias.read_bytes(),b'previous')
            self.assertEqual(record['tiles'],{});self.assertNotIn('capPairs',record)
            self.assertIn('cap disk full',record['errors'][0][2])

    def test_mode_mismatch_refused_before_world_reads_or_writes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);out=root/'out';out.mkdir()
            for stored,requested in [(False,True),(True,False)]:
                data=json.dumps({'caves':stored}).encode();(out/'index.json').write_bytes(data)
                with self.assertRaisesRegex(wh.HiresError,'separate'):
                    wh.build(root/'absent-world',root/'absent-catalog',out,1,caves=requested)
                self.assertEqual((out/'index.json').read_bytes(),data)
                self.assertEqual(sorted(p.name for p in out.iterdir()),['index.json'])

    def test_unidentified_existing_tiles_are_not_adopted(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);(out/'tiles').mkdir();(out/'tiles'/'0_0.uih').write_bytes(b'keep')
            with self.assertRaises(wh.HiresError):wh.check_render_mode(out,True)
            self.assertEqual((out/'tiles'/'0_0.uih').read_bytes(),b'keep')

    def test_resume_same_mode_and_fresh_output(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'new';wh.check_render_mode(out,True);self.assertFalse(out.exists())
            out.mkdir();(out/'index.json').write_text(json.dumps({'caves':True}))
            wh.check_render_mode(out,True)
            with self.assertRaises(wh.HiresError):wh.check_render_mode(out,1)

    def test_worker_receives_cave_mode(self):
        with tempfile.TemporaryDirectory() as d,patch('offline_catalog.Catalog.load_compiled') as catalog, \
                patch('offline_mesher.Colormap'),patch('offline_mesher.Mesher') as mesher, \
                patch('world_hires.tempfile.mkdtemp',return_value=d):
            wh._init(d,{},d,d,True)
            self.assertIs(mesher.call_args.kwargs['caves'],True)
            wh._init(d,{},d,d)
            self.assertIs(mesher.call_args.kwargs['caves'],False)

if __name__=='__main__':unittest.main()

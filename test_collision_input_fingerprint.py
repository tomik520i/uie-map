from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import collision_input_fingerprint as f
import collision_export as ce
import world_hires as wh
from test_reader import SectionBuilder,chunk_nbt,build_region,record,T_LONG,T_LIST,T_COMP,T_BARR

class FingerprintTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.path=self.root/'r.0.0.mca';self.area=wh._Rect(0,0,16,16)
    def write(self,block=1,meta=0,extra=None):
        s=SectionBuilder(0);s.set(0,0,0,block,meta)
        self.path.write_bytes(build_region({(0,0):record(chunk_nbt(0,0,[s],extra_level=extra))}))
        return f.fingerprint(self.root,self.area)
    def test_unrendered_tick_changes_do_not_invalidate_blocks(self):
        a=self.write(extra={'LastUpdate':(T_LONG,10),'Entities':(T_LIST,(T_COMP,[]))})
        raw=ce.content_fingerprint(self.root,self.area)
        b=self.write(extra={'LastUpdate':(T_LONG,20),'InhabitedTime':(T_LONG,500)})
        self.assertEqual(a,b);self.assertNotEqual(raw,ce.content_fingerprint(self.root,self.area))
    def test_ids_high_bits_metadata_and_biomes_are_all_bound(self):
        base=self.write()
        for block,meta,extra in [(2,0,None),(257,0,None),(1,3,None),(1,0,{'Biomes':(T_BARR,b'\x01'*256)})]:
            self.assertNotEqual(base,self.write(block,meta,extra))
    def test_presence_and_corruption_are_not_air(self):
        base=self.write();self.path.write_bytes(build_region({}))
        self.assertNotEqual(base,f.fingerprint(self.root,self.area))
        self.path.write_bytes(b'broken')
        with self.assertRaises(ValueError):f.fingerprint(self.root,self.area)
    def test_wrong_coordinates_and_missing_biomes_refused(self):
        self.path.write_bytes(build_region({(0,0):record(chunk_nbt(1,0))}))
        with self.assertRaises(ValueError):f.fingerprint(self.root,self.area)
        self.write(extra={'Biomes':(T_BARR,bytes(256))})
        self.path.write_bytes(build_region({(0,0):record(chunk_nbt(0,0,extra_level={'Biomes':(T_BARR,bytes(1))}))}))
        with self.assertRaises((ValueError,f.reader.FormatError)):f.fingerprint(self.root,self.area)
    def test_write_during_decode_is_refused(self):
        self.write();decode=f.reader.decode_chunk
        def mutate(root):
            chunk=decode(root)
            with self.path.open('ab') as out:out.write(b'change')
            return chunk
        with patch.object(f.reader,'decode_chunk',mutate):
            with self.assertRaisesRegex(ValueError,'changed while reading'):f.fingerprint(self.root,self.area)
    def test_selected_halo_detects_neighbour_but_not_outside(self):
        self.area=wh.tile_area(1,1)[0]
        def write_at(cx):
            s=SectionBuilder(0);s.set(0,0,0,1)
            self.path.write_bytes(build_region({(cx,4):record(chunk_nbt(cx,4,[s]))}))
            return f.fingerprint(self.root,self.area)
        # Tile1,1 includes chunk3 through8 on each axis (halo included).
        empty=write_at(20);self.assertEqual(empty,write_at(21));self.assertNotEqual(empty,write_at(3))

if __name__=='__main__':unittest.main()

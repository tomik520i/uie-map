import gzip,hashlib,struct,unittest
from unittest.mock import patch
import cave_pair as cp
from cave_cap_codec import encode,CELLS
from world_hires import encode_tile
from test_world_hires import quad

class PairTests(unittest.TestCase):
    def setUp(self):
        self.geometry=encode_tile(-64,128,[quad(-63,30,129)])
        self.caps=encode(-64,128,bytearray(CELLS),present_mask=1)
        self.doc=cp.create(self.geometry,self.caps,atlas_sha='a'*64,renderer_identity='b'*64)
    def test_valid_pair_binds_all_bytes_and_origin(self):
        self.assertEqual(cp.verify(self.doc,self.geometry,self.caps),self.doc)
        self.assertEqual(self.doc['origin'],[-64,128]);self.assertEqual(self.doc['presentMask'],1)
        self.assertEqual(cp.canonical(self.doc),cp.canonical(dict(reversed(list(self.doc.items())))))
        altered=bytearray(self.caps);altered[-1]=3
        with self.assertRaisesRegex(ValueError,'hash'):cp.verify(self.doc,self.geometry,bytes(altered))
    def test_validly_hashed_wrong_pair_or_metadata_rejected(self):
        wrong=encode(0,128,bytearray(CELLS),present_mask=1)
        with self.assertRaisesRegex(ValueError,'origins'):cp.create(self.geometry,wrong,atlas_sha='a'*64,renderer_identity='b'*64)
        for changed in [{'origin':[0,128]},{'presentMask':2}]:
            with self.assertRaisesRegex(ValueError,'metadata'):cp.verify({**self.doc,**changed},self.geometry,self.caps)
    def test_malformed_frames_and_gzip_bombs_rejected(self):
        raw=gzip.decompress(self.geometry)
        bad=[b'bad',gzip.compress(raw+b'x'),self.geometry+self.geometry,gzip.compress(raw[:24]),self.geometry[:-5]]
        for value in bad:
            with self.assertRaises(ValueError):cp.create(value,self.caps,atlas_sha='a'*64,renderer_identity='b'*64)
        with patch.object(cp,'MAX_GEOMETRY_RAW',30):
            with self.assertRaisesRegex(ValueError,'oversized'):cp.create(self.geometry,self.caps,atlas_sha='a'*64,renderer_identity='b'*64)
    def test_schema_and_missing_data_rejected(self):
        for changed in [{'extra':1},{'capBytes':True},{'presentMask':0},{'origin':[True,128]},{'capIdentity':'x'}, {'geometryBytes':-1}]:
            with self.assertRaises(ValueError):cp.validate({**self.doc,**changed})
        empty=encode(-64,128,bytearray(CELLS),present_mask=0)
        with self.assertRaisesRegex(ValueError,'known'):cp.create(self.geometry,empty,atlas_sha='a'*64,renderer_identity='b'*64)

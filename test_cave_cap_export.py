import unittest
from types import SimpleNamespace
from cave_cap_export import build_cap_tile
from cave_cap_codec import HEADER
from test_offline_renderer import volume,ASSETS,LAYOUT
from offline_catalog import Catalog

def at(raw,x,y,z):
    i=(z*64+x)*256+y
    return raw[24+i//4]>>((i%4)*2)&3

class ExportTests(unittest.TestCase):
    def test_catalog_models_air_liquid_and_partial_omissions(self):
        v=volume({(1,30,1):(1,0),(2,30,1):(9,0),(3,30,1):(31,0),
                  (4,30,1):(53,4),(5,30,1):(700,0)},x0=0,z0=0,size=64,core=(0,0,64,64))
        raw,stats=build_cap_tile(v,Catalog(ASSETS,LAYOUT))
        self.assertEqual([at(raw,x,30,1) for x in range(7)],[0,1,2,0,0,1,0])
        self.assertEqual(stats['partialModelsWithoutCap'],{'stairs':1})
        self.assertEqual(stats['fallbackCubeCells'],1)
        self.assertEqual(stats['presentChunks'],16)
    def test_missing_chunk_not_fabricated_and_registry_missing_rejected(self):
        v=volume({(1,30,1):(1,0)},x0=-64,z0=-64,size=128,core=(-64,-64,64,64))
        v.present=frozenset({(-4,-4)})
        raw,stats=build_cap_tile(v,Catalog(ASSETS,LAYOUT))
        self.assertEqual(HEADER.unpack_from(raw)[-1],1)
        self.assertEqual(stats['presentChunks'],1)
        v=volume({(1,30,1):(6553,0)},x0=0,z0=0,size=64,core=(0,0,64,64))
        with self.assertRaisesRegex(ValueError,'registry'):build_cap_tile(v,Catalog(ASSETS,LAYOUT))
    def test_wrong_core_rejected(self):
        for core in [SimpleNamespace(x=1,z=0,width=64,depth=64),SimpleNamespace(x=0,z=0,width=16,depth=64)]:
            with self.assertRaises(ValueError):build_cap_tile(SimpleNamespace(core=core),None)

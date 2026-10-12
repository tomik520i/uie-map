"""Regression: modded biome fallback colours must not leak across blocks/jobs."""
from collections import Counter
import unittest
from offline_mesher import Colormap, Mesher
from test_offline_renderer import OfflineRendererTests, volume


def colors():
    c = Colormap.__new__(Colormap)
    c.maps = {k: (256, 3, bytes([90,180,60]) * 256**2) for k in ('grass','foliage')}
    c.cache = {}; c.unknown_biomes = Counter()
    return c


class ColorDeterminismTests(unittest.TestCase):
    def test_modded_biome_preserves_each_species_in_both_orders(self):
        oak = (72/255,181/255,24/255); spruce = (97/255,153/255,97/255)
        for order in ((oak,spruce,oak),(spruce,oak,spruce)):
            c = colors()
            self.assertEqual([c.tint('foliage',76,v) for v in order], list(order))
            self.assertEqual(c.unknown_biomes[76],3)

    def test_grass_fallback_does_not_depend_on_previous_block(self):
        c = colors()
        self.assertEqual(c.tint('grass',88,(.2,.3,.4)),(.2,.3,.4))
        self.assertEqual(c.tint('grass',88,(.6,.7,.8)),(.6,.7,.8))

    def test_vanilla_and_mutated_climate_still_override_fallback(self):
        c = colors(); expected = (90/255,180/255,60/255)
        for biome in (1,129):
            for fallback in ((1.,0.,0.),(0.,0.,1.)):
                self.assertEqual(c.tint('foliage',biome,fallback),expected)
        self.assertEqual(c.unknown_biomes,{})

    def test_full_mesh_same_after_unrelated_modded_biome_tint(self):
        OfflineRendererTests.setUpClass()
        self.addCleanup(OfflineRendererTests.tearDownClass)
        v=volume({(0,60,0):(18,0)})
        v.biomes[:]=bytes([76])*len(v.biomes)
        fresh=colors(); warm=colors()
        warm.tint('foliage',76,(.9,.1,.9))
        a=Mesher(OfflineRendererTests.catalog,fresh).mesh_batch(v)
        b=Mesher(OfflineRendererTests.catalog,warm).mesh_batch(v)
        self.assertEqual(a['chunks'],b['chunks'])


if __name__=='__main__':unittest.main()

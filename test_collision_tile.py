import gzip
import unittest
from test_offline_renderer import volume
import collision_tile as ct


class CollisionTileTests(unittest.TestCase):
    def fixture(self):
        v = volume({(-63, 64, -63): (1, 0), (-63, 70, -63): (18, 0),
                    (-62, 64, -63): (9, 7)}, x0=-64, z0=-64, size=64, core=(-64,-64,64,64))
        v.registry[44] = 'minecraft:stone_slab'
        i = ((1 * 64) + 3) * 256 + 64
        v.ids[i] = 44; v.metas[i] = 8
        return v

    def test_preserves_air_below_canopy_water_metadata_and_upper_slab(self):
        raw = ct.encode_raw(self.fixture(), -64, -64)
        tile = ct.decode_raw(raw)
        self.assertEqual(ct.block_at(tile, -63, 65, -63), (0,0,'minecraft:air'))
        self.assertEqual(ct.block_at(tile, -63, 70, -63), (18,0,'minecraft:leaves'))
        self.assertEqual(ct.block_at(tile, -62, 64, -63), (9,7,'minecraft:water'))
        self.assertEqual(ct.block_at(tile, -61, 64, -63), (44,8,'minecraft:stone_slab'))
        self.assertEqual(gzip.decompress(ct.compressed(raw)), raw)
        self.assertEqual(ct.compressed(raw), ct.compressed(raw))

    def test_absent_chunk_is_unknown_not_air(self):
        v = self.fixture()
        v.present = v.present - {(-4,-4)}
        tile = ct.decode_raw(ct.encode_raw(v,-64,-64))
        self.assertIsNone(ct.block_at(tile,-63,65,-63))
        self.assertEqual(ct.block_at(tile,-47,65,-63)[0],0)
        self.assertIsNone(ct.block_at(tile,0,65,-63))

    def test_invalid_or_truncated_payload_refused(self):
        raw = ct.encode_raw(self.fixture(),-64,-64)
        for data in (raw[:10],raw[:-1],raw+b'x',b'BAD!'+raw[4:],raw[:-1]+bytes([16])):
            with self.assertRaises(ValueError): ct.decode_raw(data)
        with self.assertRaises(ValueError): ct.encode_raw(self.fixture(),-63,-64)


if __name__ == '__main__': unittest.main()

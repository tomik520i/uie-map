"""Tests of surface_color_diff on synthetic but real UIH1 payloads (world_hires.encode_tile)."""
import gzip
import json
import unittest

import surface_color_diff as scd
import world_hires as wh

KEY = '1,-2'
X, Z = 64, -128


def part(dx=0, *, dy=0.0, u=0.0, color=0.5, transparent=False, vertex_colors=None):
    x, z = X + dx, Z
    doc = {'positions': [x, 64 + dy, z, x + 1, 64, z, x + 1, 64, z + 1, x, 64, z + 1],
           'uvs': [u, 0, 1, 0, 1, 1, 0, 1], 'colors': vertex_colors or [color] * 12}
    if transparent:
        doc['transparent'] = True
    return doc


def tile(*parts, origin=(X, Z)):
    first, *rest = parts
    return wh.encode_tile(origin[0], origin[1], [{**first, 'transparentParts': list(rest)}])


class SurfaceColorDiffTests(unittest.TestCase):
    def setUp(self):
        self.base = tile(part(0), part(1), part(2, transparent=True))

    def classify(self, new, old=None):
        return scd.compare_tiles(KEY, self.base if old is None else old, new)

    def test_identical_and_compression_only(self):
        same = self.classify(self.base)
        self.assertEqual(same['class'], 'identical')
        self.assertEqual(same['differingColorBytes'], 0)
        recompressed = gzip.compress(gzip.decompress(self.base), compresslevel=1, mtime=0)
        self.assertNotEqual(recompressed, self.base)
        result = self.classify(recompressed)
        self.assertEqual(result['class'], 'compression-only')
        self.assertEqual(result['old']['rawSha256'], result['new']['rawSha256'])
        self.assertNotEqual(result['old']['sha256'], result['new']['sha256'])
        json.dumps(result)

    def test_colors_only_with_exact_byte_count(self):
        changed = tile(part(0), part(1, vertex_colors=[0.9, 0.9, 0.9] + [0.5] * 9), part(2, transparent=True))
        result = self.classify(changed)
        self.assertEqual(result['class'], 'colors-only')
        self.assertTrue(result['positionsEqual'] and result['uvsEqual'])
        self.assertFalse(result['colorsEqual'])
        self.assertEqual(result['differingColorBytes'], 3, 'one vertex, three channels')
        translucent = tile(part(0), part(1), part(2, transparent=True, color=0.1))
        self.assertEqual(self.classify(translucent)['differingColorBytes'], 12, 'translucent group compared too')
        self.assertEqual(result['old']['counts'], [2, 1])
        json.dumps(result)

    def test_position_or_uv_changes_are_structural(self):
        moved = self.classify(tile(part(0), part(1, dy=0.5), part(2, transparent=True)))
        self.assertEqual((moved['class'], moved['positionsEqual']), ('geometry-or-uv', False))
        self.assertIn('position', moved['reason'])
        both = self.classify(tile(part(0), part(1, dy=0.5, color=0.9), part(2, transparent=True)))
        self.assertEqual(both['class'], 'geometry-or-uv', 'a colour change never hides a structural one')
        uv = self.classify(tile(part(0), part(1, u=0.25), part(2, transparent=True)))
        self.assertEqual((uv['class'], uv['positionsEqual'], uv['uvsEqual']), ('geometry-or-uv', True, False))

    def test_group_counts_differ(self):
        moved_group = self.classify(tile(part(0), part(1, transparent=True), part(2, transparent=True)))
        self.assertEqual(moved_group['class'], 'geometry-or-uv')
        self.assertEqual((moved_group['old']['counts'], moved_group['new']['counts']), ([2, 1], [1, 2]))
        self.assertIn('counts', moved_group['reason'])
        extra = self.classify(tile(part(0), part(1), part(3), part(2, transparent=True)))
        self.assertEqual(extra['class'], 'geometry-or-uv')

    def test_invalid_payloads_are_refused(self):
        other_tile = tile(part(64), origin=(X + 64, Z))                # valid UIH1 of the neighbouring tile
        raw = gzip.decompress(self.base)
        bad = {
            'origin mismatch': other_tile,
            'not gzip': b'garbage',
            'trailing bytes': self.base + b'x',
            'truncated': self.base[:-5],
            'second gzip member': self.base + self.base,
            'header only': gzip.compress(wh.HEADER.pack(wh.MAGIC, 1, 0, X, Z, 0, 0), mtime=0),
            'count mismatch': gzip.compress(raw[:-1], mtime=0),
            'wrong magic': gzip.compress(b'XXXX' + raw[4:], mtime=0),
            'empty': b'',
            'not bytes': 'text',
        }
        for label, payload in bad.items():
            with self.subTest(label):
                with self.assertRaises(scd.SurfaceDiffError):
                    self.classify(payload)
                with self.assertRaises(scd.SurfaceDiffError):
                    scd.compare_tiles(KEY, payload, self.base)
        with self.assertRaises(scd.SurfaceDiffError):
            scd.compare_tiles('01,0', self.base, self.base)

    def test_size_limits_are_enforced(self):
        with self.assertRaisesRegex(scd.SurfaceDiffError, 'size outside'):
            scd.compare_tiles(KEY, self.base, self.base, max_compressed=10)
        with self.assertRaisesRegex(scd.SurfaceDiffError, 'exceeds limit'):
            scd.compare_tiles(KEY, self.base, self.base, max_raw=wh.HEADER.size + 10)
        with self.assertRaises(scd.SurfaceDiffError):
            scd.compare_tiles(KEY, self.base, self.base, max_raw=0)


if __name__ == '__main__':
    unittest.main()

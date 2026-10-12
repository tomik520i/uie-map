"""Tests for world_diagnostics.py with the real catalog, mesher and volume fixtures of test_offline_renderer
(imported unmodified). Stdlib only, Windows/Linux.
    python -m unittest test_world_diagnostics -v      (from work/uie-map)
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import png_io
from offline_catalog import Catalog
from offline_mesher import Colormap, Mesher
from test_offline_renderer import ASSETS, LAYOUT, block, volume
import world_diagnostics as wd

# Extra blocks for the diagnostics (a copy: the shared fixture stays untouched).
EXTRA = {
    'mod:half_textured': (900, block(0, True, 'net.minecraft.block.BlockStone', ['stone'] * 5 + [None])),
    'mod:missing_icon': (901, block(0, True, 'net.minecraft.block.BlockStone', ['missingno'] + ['stone'] * 5)),
    'mod:ghost': (902, block(-1, False, 'mod.Ghost', ['stone'] * 6)),
    'mod:plain': (903, block(0, True, 'mod.Plain', ['stone'] * 6)),
    'mod:plain_hole': (904, block(0, True, 'mod.Plain', ['stone'] * 5 + [None])),
}
STONE, MYSTERY = 1, 700


def catalog():
    assets = copy.deepcopy(ASSETS)
    for name, (_id, spec) in EXTRA.items():
        assets['blocks'][name] = spec
    return Catalog(assets, copy.deepcopy(LAYOUT))


def diag_volume(blocks, **kw):
    v = volume(blocks, **kw)
    for name, (block_id, _spec) in EXTRA.items():
        v.registry[block_id] = name
    return v


def entry(doc, block_id, meta=0):
    return next(e for e in doc['entries'] if e['id'] == block_id and e['meta'] == meta)


def buried(x, y, z):
    """Stone on all six sides of (x, y, z)."""
    return {(x + dx, y + dy, z + dz): (STONE, 0)
            for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))}


class TileDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        px = bytes([90, 180, 60]) * (256 * 256)
        for name in ('grass.png', 'foliage.png'):
            (d / name).write_bytes(png_io.encode(256, 256, 3, px))
        cls.catalog = catalog()
        cls.mesher = Mesher(cls.catalog, Colormap(d / 'grass.png', d / 'foliage.png'), caves=False)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_tile(self, blocks, with_stats=True, **kw):
        vol = diag_volume(blocks, **kw)
        stats = self.mesher.mesh_batch(vol)['stats'] if with_stats else None
        return wd.validate_diagnostics(wd.tile_diagnostics(vol, self.catalog, stats))

    def test_fallback_all_vs_visible_buried_and_halo_excluded(self):
        blocks = {(1, 64, 1): (MYSTERY, 0), (5, 10, 5): (MYSTERY, 0), **buried(5, 10, 5),
                  (-5, 64, -5): (MYSTERY, 0), (20, 64, 20): (MYSTERY, 0)}          # last two: halo, not core
        doc = self.run_tile(blocks)
        e = entry(doc, MYSTERY)
        self.assertEqual((e['name'], e['class'], e['all'], e['visible']), ('mod:mystery', 'fallback', 2, 1))
        self.assertIn('no rule for renderType 999', e['reason'])
        self.assertEqual(e['sample'], [1, 64, 1], 'first core voxel in scan order (chunk, z, x, lowest y)')
        self.assertEqual(doc['classes']['fallback'], {'all': 2, 'visible': 1, 'lowerBound': False})
        self.assertEqual(doc['coverage']['coreChunks'], 1)
        self.assertEqual(doc['coverage']['voxels'], 2 + 6)
        self.assertTrue(doc['coverage']['visibleKnown'])
        self.assertFalse(any(x['id'] == STONE for x in doc['entries']), 'normal verified blocks are not listed')

    def test_without_mesher_stats_visible_is_unknown(self):
        doc = self.run_tile({(1, 64, 1): (MYSTERY, 0)}, with_stats=False)
        self.assertIsNone(entry(doc, MYSTERY)['visible'])
        self.assertIsNone(doc['classes']['fallback']['visible'])
        self.assertEqual(doc['classes']['invisible']['visible'], 0, 'no such block: exactly zero')
        self.assertFalse(doc['coverage']['visibleKnown'])

    def test_missing_texture_on_a_verified_model_and_explicit_missing_icon(self):
        doc = self.run_tile({(1, 64, 1): (900, 0), (2, 64, 1): (901, 0)})
        for block_id in (900, 901):
            e = entry(doc, block_id)
            self.assertEqual(e['class'], 'missingTexture')
            self.assertEqual(e['flags'], ['missingTexture'])
            self.assertIn('1 of 6 model faces use the missing texture', e['reason'])
            self.assertIsNone(e['visible'], 'the mesher does not count emitted missing-texture blocks')
        self.assertEqual(doc['classes']['missingTexture']['all'], 2)

    def test_invisible_assumed_and_overlap_keep_evidence(self):
        doc = self.run_tile({(1, 64, 1): (902, 0), (2, 64, 1): (903, 0), (3, 64, 1): (904, 0), (4, 64, 1): (0, 0)})
        self.assertEqual((entry(doc, 902)['class'], entry(doc, 902)['reason']), ('invisible', 'renderType -1'))
        self.assertEqual(entry(doc, 903)['class'], 'assumed')
        overlap = entry(doc, 904)
        self.assertEqual(overlap['class'], 'missingTexture', 'problem class wins over assumed')
        self.assertEqual(overlap['flags'], ['assumed', 'missingTexture'])
        self.assertIn('unlisted class', overlap['reason'])
        self.assertFalse(any(e['id'] == 0 for e in doc['entries']), 'air is never listed')

    def test_ids_meta_and_unknown_ids(self):
        doc = self.run_tile({(1, 64, 1): (MYSTERY, 3), (2, 64, 1): (MYSTERY, 0), (3, 64, 1): (4000, 2),
                             (4, 64, 1): (4001, 2)})
        self.assertEqual(entry(doc, MYSTERY, 3)['meta'], 3)
        self.assertEqual(entry(doc, MYSTERY, 0)['all'], 1)
        unknown = entry(doc, 4000, 2)
        self.assertEqual((unknown['name'], unknown['class']), (None, 'fallback'))
        self.assertEqual(unknown['flags'], ['fallback', 'missingTexture', 'unknownId'])
        self.assertIn('no exported asset', unknown['reason'])
        self.assertIsNone(unknown['visible'], 'the mesher counts both unknown ids under (None, 2): ambiguous')
        vol = diag_volume({(1, 64, 1): (5000, 0)})
        vol.registry[5000] = 'uie:unknown_5000'                  # world_hires._batch placeholder
        named = entry(wd.tile_diagnostics(vol, self.catalog), 5000)
        self.assertEqual((named['name'], named['class']), ('uie:unknown_5000', 'fallback'))
        self.assertIn('unknownId', named['flags'])

    def test_tile_truncation_is_deterministic_and_counted(self):
        blocks = {(i % 16, 64 + i // 16, 1): (MYSTERY, i % 16) for i in range(16)}
        blocks.update({(i, 70, 2): (MYSTERY, 0) for i in range(5)})              # meta 0 most frequent
        vol = diag_volume(blocks)
        doc = wd.tile_diagnostics(vol, self.catalog, None, limit=4)
        self.assertEqual([e['meta'] for e in doc['entries']], [0, 1, 2, 3])
        self.assertEqual(doc['truncated']['entries'], 12)
        self.assertEqual(doc['truncated']['all'], 12)
        self.assertEqual(doc['classes']['fallback']['all'], 21, 'class totals include the truncated keys')
        self.assertEqual(wd.tile_diagnostics(vol, self.catalog, None, limit=4), doc)

    def test_empty_tile_and_limits(self):
        empty = wd.validate_diagnostics(wd.empty_tile_diagnostics())
        self.assertEqual((empty['coverage']['coreChunks'], empty['entries']), (0, []))
        for limit in (0, 501, 'x'):
            with self.assertRaises(wd.DiagnosticsError):
                wd.tile_diagnostics(diag_volume({}), self.catalog, limit=limit)


def tile_doc(entries, visible_known=True, truncated=0):
    classes = {c: {'all': 0, 'visible': 0, 'lowerBound': False} for c in wd.CLASSES}
    for e in entries:
        classes[e['class']]['all'] += e['all']
        if e['visible'] is None:
            classes[e['class']]['visible'] = None
        elif classes[e['class']]['visible'] is not None:
            classes[e['class']]['visible'] += e['visible']
    return {'format': wd.FORMAT, 'version': 1, 'scope': 'tile', 'classes': classes, 'entries': entries,
            'coverage': {'tiles': 1, 'expectedTiles': 1, 'coreChunks': 16, 'coreChunksExpected': 16,
                         'voxels': 1000, 'visibleKnown': visible_known, 'complete': True},
            'truncated': {'entries': truncated, 'all': truncated * 5, 'inputParts': 0, 'inputEntries': 0, 'inputAll': 0}}


def fb(block_id, all_, visible, sample, meta=0):
    return {'id': block_id, 'meta': meta, 'name': f'mod:b{block_id}', 'class': 'fallback', 'flags': ['fallback'],
            'reason': 'no rule', 'all': all_, 'visible': visible, 'sample': sample, 'lowerBound': False}


class AggregationTests(unittest.TestCase):
    def test_merge_is_order_independent_and_sums_visible_only_when_known(self):
        a = tile_doc([fb(1, 5, 2, [10, 5, 3]), fb(2, 1, 1, [0, 0, 0])])
        b = tile_doc([fb(1, 3, 1, [2, 9, 9])])
        c = tile_doc([fb(1, 4, None, [50, 1, 1])], visible_known=False)
        job = wd.job_diagnostics([a, b], expected_tiles=2)
        self.assertEqual(job, wd.job_diagnostics([b, a], expected_tiles=2))
        one = entry(job, 1)
        self.assertEqual((one['all'], one['visible'], one['sample'], one['lowerBound']), (8, 3, [2, 9, 9], False))
        self.assertTrue(job['coverage']['complete'])
        mixed = wd.job_diagnostics([a, b, c], expected_tiles=3)
        self.assertEqual(entry(mixed, 1)['all'], 12)
        self.assertIsNone(entry(mixed, 1)['visible'], 'one unknown part: no manufactured full visible count')
        self.assertIsNone(mixed['classes']['fallback']['visible'])
        self.assertFalse(mixed['coverage']['visibleKnown'])

    def test_truncated_inputs_make_absent_keys_lower_bounds(self):
        a = tile_doc([fb(1, 5, 2, [0, 0, 0])], truncated=3)            # may contain key 2 without listing it
        b = tile_doc([fb(2, 4, 4, [1, 1, 1])])
        job = wd.job_diagnostics([a, b], expected_tiles=2)
        self.assertFalse(entry(job, 1)['lowerBound'])
        self.assertTrue(entry(job, 2)['lowerBound'])
        self.assertEqual(job['truncated'], {'entries': 0, 'all': 0, 'inputParts': 1, 'inputEntries': 3, 'inputAll': 15})

    def test_merge_truncation_is_deterministic_with_explicit_counts(self):
        tiles = [tile_doc([fb(i, 10 - i % 3, 0, [i, 0, 0])]) for i in range(1, 8)]
        job = wd.job_diagnostics(tiles, expected_tiles=64, limit=3)
        self.assertEqual([e['id'] for e in job['entries']], [3, 6, 1])          # all desc (10, 10, 9), then id
        self.assertEqual(job['truncated']['entries'], 4)
        self.assertEqual(job['classes']['fallback']['all'], sum(10 - i % 3 for i in range(1, 8)))

    def test_partially_covered_jobs_are_not_complete(self):
        job = wd.job_diagnostics([tile_doc([])] * 63, expected_tiles=64)
        self.assertEqual((job['coverage']['tiles'], job['coverage']['expectedTiles']), (63, 64))
        self.assertFalse(job['coverage']['complete'], 'a failed tile leaves the job incomplete')
        with self.assertRaises(wd.DiagnosticsError):
            wd.job_diagnostics([tile_doc([])] * 3, expected_tiles=2)

    def test_old_jobs_without_diagnostics_stay_unknown(self):
        full = wd.job_diagnostics([tile_doc([fb(1, 5, 5, [0, 0, 0])])], expected_tiles=1)
        partial = wd.job_diagnostics([tile_doc([])], expected_tiles=2)
        world = wd.validate_diagnostics(wd.world_diagnostics([full, partial, None, None], planned_jobs=5))
        self.assertEqual(world['jobs'], {'planned': 5, 'withDiagnostics': 2, 'withoutDiagnostics': 3, 'completeJobs': 1})
        self.assertFalse(world['coverage']['complete'])
        self.assertTrue(entry(world, 1)['lowerBound'], 'jobs without diagnostics may contain more')
        self.assertTrue(world['classes']['invisible']['lowerBound'], 'unknown, not zero')
        clean = wd.world_diagnostics([full], planned_jobs=1)
        self.assertTrue(clean['coverage']['complete'])
        self.assertFalse(entry(clean, 1)['lowerBound'])
        with self.assertRaises(wd.DiagnosticsError):
            wd.world_diagnostics([full, full], planned_jobs=1)

    def test_validation_refuses_malformed_or_oversized_docs(self):
        good = tile_doc([fb(1, 5, 5, [0, 0, 0])])
        wd.validate_diagnostics(json.loads(json.dumps(good)))
        bad = []
        for mutate in (lambda d: d.update(format='X'), lambda d: d.update(scope='region'),
                       lambda d: d['entries'][0].update(id=-1), lambda d: d['entries'][0].update(meta=256),
                       lambda d: d['entries'][0].update(flags=['assumed']), lambda d: d['entries'][0].update(visible=-1),
                       lambda d: d['entries'][0].update(sample=[0, 0]), lambda d: d['entries'][0].update(reason='x' * 301),
                       lambda d: d['coverage'].update(voxels=-1), lambda d: d['classes'].pop('assumed'),
                       lambda d: d.update(entries=[d['entries'][0]] * 501)):
            doc = copy.deepcopy(good)
            mutate(doc)
            bad.append(doc)
        for doc in bad:
            with self.assertRaises(wd.DiagnosticsError):
                wd.validate_diagnostics(doc)
        with self.assertRaises(wd.DiagnosticsError):
            wd.world_diagnostics([good], planned_jobs=1)                       # tile doc where a job doc belongs


if __name__ == '__main__':
    unittest.main()

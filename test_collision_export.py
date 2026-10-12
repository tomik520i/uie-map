"""Tests for collision_export.py with tiny fixture volumes and an injected renderer (no world, no Minecraft).
    python -m unittest test_collision_export -v      (from work/uie-map)
"""
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import collision_export as ce
import test_collision_tile as fixtures
from test_offline_renderer import volume
from test_world_hires import quad
import world_hires as wh

ATLAS = 'a' * 64


def geometry_sha12(vol, mesh):
    docs = [doc for _, doc in sorted(mesh['chunks'].items())]
    return hashlib.sha256(wh.encode_tile(vol.core.x, vol.core.z, docs)).hexdigest()[:12]


class ExportHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.out, self.hires = base / 'out', base / 'hires'
        self.hires.mkdir()
        # Tile -1,-1 (negative coordinates) and tile 0,0, each one Volume + its mesh result.
        self.tiles = {
            '-1,-1': (fixtures.CollisionTileTests().fixture(), {'chunks': {(-4, -4): quad(-63, 64, -63)}}),
            '0,0': (volume({(1, 64, 1): (1, 0)}, x0=0, z0=0, size=64, core=(0, 0, 64, 64)),
                    {'chunks': {(0, 0): quad(1, 64, 1)}}),
        }
        self.calls = []
        self.behaviour = {}
        self.write_hires({key: geometry_sha12(*vm) for key, vm in self.tiles.items()})

    def write_hires(self, tiles, atlas=ATLAS, **extra):
        doc = {'format': 'UIE_HIRES_INDEX1', 'tileSize': 64, 'atlas': 'atlas.png', 'atlasSha256': atlas,
               'tiles': tiles, **extra}
        (self.hires / 'index.json').write_text(json.dumps(doc), encoding='utf-8')

    def render(self, tx, tz):
        key = f'{tx},{tz}'
        self.calls.append(key)
        action = self.behaviour.get(key)
        if action:
            return action()
        return self.tiles[key]

    def run_export(self, keys, deferred=(), **kw):
        options = dict(out=self.out, hires_store=self.hires, render=self.render, catalog_atlas_sha256=ATLAS,
                       min_free_bytes=0, disk_free=lambda _p: 10 ** 12)
        options.update(kw)
        tiles = [tuple(int(v) for v in k.split(',')) for k in keys]
        return ce.export_tiles(tiles, [tuple(int(v) for v in k.split(',')) for k in deferred], **options)

    def pointer(self):
        path = self.out / 'store' / 'current.json'
        return path.read_bytes() if path.exists() else None

    def index_pairs(self):
        sha = json.loads(self.pointer())['indexSha256']
        return json.loads((self.out / 'store' / 'indexes' / f'{sha}.json').read_bytes())['pairs']


class ExportTests(ExportHarness):
    def test_publish_then_reuse_without_rendering(self):
        first = self.run_export(['-1,-1', '0,0'])
        self.assertEqual((first['state'], first['published'], first['completed'], first['requested']), ('done', 2, 2, 2))
        self.assertTrue(first['indexPublished'])
        self.assertEqual(set(self.index_pairs()), {'-1,-1', '0,0'})
        written = json.loads((self.out / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(written, first)
        self.assertRegex(written['startedAt'], r'^\d{4}-\d\d-\d\dT')
        self.assertGreater(first['newBytes'], 0)
        second = self.run_export(['-1,-1', '0,0'])
        self.assertEqual((second['state'], second['reused'], second['published']), ('done', 2, 0))
        self.assertEqual(self.calls, ['-1,-1', '0,0'], 'reuse does not render again')
        self.assertFalse(second['indexPublished'])
        self.assertEqual(second['indexSha256'], first['indexSha256'])

    def test_changed_hires_revision_refuses_tile_and_drops_old_pair_from_current(self):
        self.run_export(['-1,-1', '0,0'])
        old_pair = self.index_pairs()['0,0']
        tiles = {key: geometry_sha12(*vm) for key, vm in self.tiles.items()}
        tiles['0,0'] = 'f' * 12                      # a newer visible revision this source cannot produce
        self.write_hires(tiles)
        status = self.run_export(['-1,-1', '0,0'])
        self.assertEqual(status['tiles']['-1,-1']['result'], 'reused')
        self.assertEqual(status['tiles']['0,0']['result'], 'refused')
        self.assertIn('differs from hires', status['tiles']['0,0']['reason'])
        self.assertEqual(status['staleDropped'], 1)
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(set(self.index_pairs()), {'-1,-1'}, 'the stale pair is no longer declared current')
        self.assertTrue((self.out / 'store' / 'pairs' / f'{old_pair}.json').exists(), 'but it stays stored')

    def test_hires_changed_during_the_computation_is_refused(self):
        original = self.tiles['0,0']
        def render_while_hires_changes():
            self.write_hires({'0,0': 'e' * 12})
            return original
        self.behaviour['0,0'] = render_while_hires_changes
        status = self.run_export(['0,0'])
        self.assertEqual(status['tiles']['0,0'], {'result': 'refused',
                                                  'reason': 'hires tile or atlas changed during the export'})
        self.assertFalse((self.out / 'store' / 'pairs').exists(), 'nothing published')
        self.assertIsNone(self.pointer())
        self.assertEqual(status['state'], 'partial')

    def test_source_failure_keeps_previous_good_pairs(self):
        self.run_export(['0,0'])
        before = self.pointer()
        def corrupted():
            raise ce.TileError('source read failed for 1 chunk(s) of the tile input (halo included), e.g. (-5, -4)')
        self.behaviour['-1,-1'] = corrupted
        status = self.run_export(['-1,-1', '0,0'])
        self.assertEqual((status['failed'], status['reused'], status['completed']), (1, 1, 1))
        self.assertEqual(status['state'], 'partial', 'a partial failure is never done')
        self.assertIn('source read failed', status['errors'][0]['reason'])
        self.assertEqual(self.pointer(), before, 'index unchanged, the good pair stays current')
        self.assertEqual(set(self.index_pairs()), {'0,0'})

    def test_tile_absent_from_hires_is_skipped_as_unknown(self):
        self.write_hires({'0,0': geometry_sha12(*self.tiles['0,0'])})
        status = self.run_export(['-1,-1'])
        self.assertEqual(status['tiles']['-1,-1']['result'], 'skipped')
        self.assertIn('no pair is invented', status['tiles']['-1,-1']['reason'])
        self.assertEqual(self.calls, [])
        self.assertEqual(status['state'], 'partial')

    def test_stale_hires_tile_and_atlas_mismatch_are_refused(self):
        self.write_hires({key: geometry_sha12(*vm) for key, vm in self.tiles.items()}, stale=['0,0'])
        self.assertEqual(self.run_export(['0,0'])['tiles']['0,0']['result'], 'refused')
        status = self.run_export(['-1,-1'], catalog_atlas_sha256='b' * 64)
        self.assertTrue(status['state'].startswith('failed: catalog atlas differs'))
        self.assertEqual(self.calls, [])

    def test_previous_index_kept_when_index_publication_fails(self):
        self.run_export(['-1,-1'])
        before = self.pointer()
        def failing(store, pairs):
            raise OSError('simulated full disk')
        status = self.run_export(['0,0'], publish_index=failing)
        self.assertTrue(status['state'].startswith('failed: index publication failed'))
        self.assertFalse(status['indexPublished'])
        self.assertEqual(self.pointer(), before)
        self.assertEqual(set(self.index_pairs()), {'-1,-1'})

    def test_budget_and_free_space_guards(self):
        status = self.run_export(['0,0'], budget_bytes=1)
        self.assertTrue(status['state'].startswith('failed: budget'))
        self.assertEqual(self.calls, [], 'no work beyond the budget estimate')
        # The estimate passes, the exact new payload does not: stop cleanly before publishing anything.
        status = self.run_export(['0,0', '-1,-1'], ['5,5'], budget_bytes=ce.metadata_reserve(2) + 2048,
                                 estimate_tile_bytes=0)
        self.assertTrue(status['budgetStopped'])
        self.assertEqual(status['tiles']['0,0']['result'], 'skipped')
        self.assertEqual(status['tiles']['-1,-1']['reason'], 'not started: budget')
        self.assertIn('deferred', status['tiles']['5,5']['reason'])
        self.assertFalse((self.out / 'store' / 'pairs').exists())
        self.assertEqual(status['state'], 'partial')
        status = self.run_export(['0,0'], min_free_bytes=10 ** 13)
        self.assertTrue(status['state'].startswith('failed: free disk'))

    def test_crash_is_reported_as_failed_and_raised(self):
        with patch.object(ce, 'read_hires_index', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_export(['0,0'])
        written = json.loads((self.out / 'status.json').read_text(encoding='utf-8'))
        self.assertTrue(written['state'].startswith('failed: crashed'))
        self.assertIsNotNone(written['finishedAt'])


class ReviewRegressionTests(ExportHarness):
    """Task 103: object verification before reuse/merge, stale in every comparison, budget stop from the copies."""

    def blocks_object(self, key):
        pair_sha = self.index_pairs()[key]
        pair = json.loads((self.out / 'store' / 'pairs' / f'{pair_sha}.json').read_bytes())
        return self.out / 'store' / 'objects' / f"{pair['blocksSha256']}.uib"

    def test_corrupted_existing_uib_is_reported_not_reused_and_not_overwritten(self):
        self.run_export(['0,0'])
        uib = self.blocks_object('0,0')
        uib.write_bytes(gzip.compress(b'corrupted block object'))
        damaged = uib.read_bytes()
        status = self.run_export(['0,0'])
        self.assertEqual(status['reused'], 0, 'a pair with damaged objects is never reused')
        self.assertEqual((status['corruptPairs'], status['corruptDropped'], status['failed']), (1, 1, 1))
        self.assertIn('immutable object differs', status['tiles']['0,0']['reason'])
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(uib.read_bytes(), damaged, 'the damaged object is reported, never overwritten')
        self.assertTrue(status['indexPublished'])
        self.assertEqual(self.index_pairs(), {}, 'the damaged pair is no longer declared current')

    def test_missing_existing_uib_is_rewritten_and_verified(self):
        self.run_export(['0,0'])
        before = self.pointer()
        uib = self.blocks_object('0,0')
        uib.unlink()
        status = self.run_export(['0,0'])
        self.assertEqual((status['corruptPairs'], status['repaired'], status['published']), (1, 1, 1))
        self.assertIn('missing objects/', status['errors'][0]['reason'])
        self.assertIn('republished', status['tiles']['0,0']['reason'])
        self.assertTrue(uib.is_file())
        self.assertIsNone(ce.verify_pair_objects(self.out / 'store', '0,0', self.index_pairs()['0,0']))
        self.assertEqual(self.pointer(), before, 'same pair hash: the index stays, now with intact objects')
        self.assertEqual(status['state'], 'done')

    def test_damaged_unselected_previous_pair_is_dropped_from_the_merge(self):
        self.run_export(['-1,-1', '0,0'])
        self.blocks_object('-1,-1').write_bytes(b'not gzip')
        status = self.run_export(['0,0'])
        self.assertEqual(status['tiles']['0,0']['result'], 'reused')
        self.assertEqual((status['corruptDropped'], status['corruptPairs']), (1, 1))
        self.assertEqual(status['errors'][0]['tile'], '-1,-1')
        self.assertEqual(set(self.index_pairs()), {'0,0'})
        self.assertEqual(status['state'], 'partial', 'never done while a current pair had to be dropped')

    def test_tile_becoming_stale_during_render_is_refused(self):
        tiles = {key: geometry_sha12(*vm) for key, vm in self.tiles.items()}
        original = self.tiles['0,0']
        def render_while_hires_marks_stale():
            self.write_hires(tiles, stale=['0,0'])      # same sha12, but now a previous (failed) revision
            return original
        self.behaviour['0,0'] = render_while_hires_marks_stale
        status = self.run_export(['0,0'])
        self.assertEqual(status['tiles']['0,0']['result'], 'refused')
        self.assertFalse((self.out / 'store' / 'pairs').exists())

    def test_tile_becoming_stale_before_the_merge_is_not_indexed(self):
        tiles = {key: geometry_sha12(*vm) for key, vm in self.tiles.items()}
        def publish_then_stale(store, volume, mesh, atlas):
            pair = ce.cb.publish_pair(store, volume, mesh, atlas)
            self.write_hires(tiles, stale=['0,0'])
            return pair
        status = self.run_export(['0,0'], publish_pair=publish_then_stale)
        self.assertEqual(status['tiles']['0,0']['result'], 'refused')
        self.assertIn('stale', status['tiles']['0,0']['reason'])
        self.assertEqual((status['published'], status['completed']), (0, 0))
        self.assertIsNone(self.pointer(), 'nothing declared current')

    def test_previous_pair_of_a_tile_now_stale_is_neither_reused_nor_kept(self):
        self.run_export(['-1,-1', '0,0'])
        self.write_hires({key: geometry_sha12(*vm) for key, vm in self.tiles.items()}, stale=['-1,-1'])
        self.assertFalse(ce.pair_matches(
            ce.read_pair(self.out / 'store', self.index_pairs()['-1,-1']), '-1,-1', ce.read_hires_index(self.hires)))
        status = self.run_export(['0,0'])
        self.assertEqual(status['staleDropped'], 1)
        self.assertEqual(set(self.index_pairs()), {'0,0'})

    def test_region_copy_budget_stops_the_run_cleanly(self):
        def over_budget():
            raise ce.CopyBudgetError('budget: region copies need 900000000 bytes')
        self.behaviour['0,0'] = over_budget
        status = self.run_export(['0,0', '-1,-1'])
        self.assertTrue(status['budgetStopped'])
        self.assertEqual(status['tiles']['-1,-1']['reason'], 'not started: budget')
        self.assertEqual(self.calls, ['0,0'])
        self.assertEqual(status['state'], 'partial')


class ProvenanceTests(ExportHarness):
    """Task 105: source input revisions bound to exact pairs decide reuse, on top of geometry/atlas/object checks."""

    def setUp(self):
        super().setUp()
        self.revisions = {'-1,-1': '1' * 64, '0,0': '2' * 64}
        self.revision_calls = []

    def source(self, tx, tz):
        key = f'{tx},{tz}'
        self.revision_calls.append(key)
        value = self.revisions[key]
        if isinstance(value, Exception):
            raise value
        return value

    def export(self, keys, **kw):
        return self.run_export(keys, source_revision=self.source, **kw)

    def provenance_bytes(self):
        return (self.out / 'provenance.json').read_bytes()

    def provenance(self):
        return json.loads(self.provenance_bytes())['tiles']

    def hide_block_change(self):
        """Tile 0,0 gets an underground block: same mesh (same UIH), different UIB, new source revision."""
        changed = volume({(1, 64, 1): (1, 0), (5, 10, 5): (1, 0)}, x0=0, z0=0, size=64, core=(0, 0, 64, 64))
        self.tiles['0,0'] = (changed, self.tiles['0,0'][1])
        self.assertEqual(geometry_sha12(*self.tiles['0,0']), ce.read_hires_index(self.hires)['tiles']['0,0'])
        self.revisions['0,0'] = '3' * 64

    def test_unchanged_inputs_are_reused_without_rendering(self):
        first = self.export(['-1,-1', '0,0'])
        self.assertEqual((first['state'], first['published']), ('done', 2))
        pairs = self.index_pairs()
        self.assertEqual(self.provenance(), {'-1,-1': {'source': '1' * 64, 'pair': pairs['-1,-1']},
                                             '0,0': {'source': '2' * 64, 'pair': pairs['0,0']}})
        saved = self.provenance_bytes()
        second = self.export(['-1,-1', '0,0'])
        self.assertEqual((second['state'], second['reused'], second['sourceRerendered']), ('done', 2, 0))
        self.assertEqual(self.calls, ['-1,-1', '0,0'], 'no render for unchanged inputs')
        self.assertEqual(self.revision_calls, ['-1,-1', '-1,-1', '0,0', '0,0', '-1,-1', '0,0'],
                         'read before and after each render; once per reused tile')
        self.assertEqual(self.provenance_bytes(), saved)

    def test_hidden_block_change_under_an_unchanged_surface_creates_a_new_pair(self):
        self.export(['-1,-1', '0,0'])
        old = self.index_pairs()['0,0']
        self.hide_block_change()
        status = self.export(['0,0'])
        new = self.index_pairs()['0,0']
        self.assertNotEqual(new, old, 'new UIB -> new pair')
        self.assertEqual((status['state'], status['published'], status['sourceRerendered']), ('done', 1, 1))
        self.assertTrue(status['indexPublished'])
        self.assertIn('re-rendered', status['tiles']['0,0']['reason'])
        self.assertEqual(self.provenance()['0,0'], {'source': '3' * 64, 'pair': new})
        self.assertEqual(self.provenance()['-1,-1']['source'], '1' * 64, 'unselected tiles keep their proof')
        again = self.export(['0,0'])
        self.assertEqual(again['reused'], 1)

    def test_source_change_during_the_render_is_refused(self):
        self.export(['-1,-1', '0,0'])
        before, saved = self.pointer(), self.provenance_bytes()
        self.hide_block_change()
        tile = self.tiles['0,0']
        def render_while_saved():
            self.revisions['0,0'] = '4' * 64
            return tile
        self.behaviour['0,0'] = render_while_saved
        status = self.export(['0,0'])
        self.assertEqual(status['tiles']['0,0'], {'result': 'refused', 'reason': 'source inputs changed during the export'})
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(self.pointer(), before)
        self.assertEqual(self.provenance_bytes(), saved, 'no proof for a refused tile')

    def test_failed_index_publication_does_not_advance_provenance(self):
        self.export(['-1,-1', '0,0'])
        saved = self.provenance_bytes()
        self.hide_block_change()
        def failing(store, pairs):
            raise OSError('simulated full disk')
        status = self.export(['0,0'], publish_index=failing)
        self.assertTrue(status['state'].startswith('failed: index publication failed'))
        self.assertFalse(status['provenance']['saved'])
        self.assertEqual(self.provenance_bytes(), saved, 'proof is persisted only after the index is current')
        renders = len(self.calls)
        status = self.export(['0,0'])
        self.assertEqual(len(self.calls), renders + 1, 'not reused: the old proof names another revision')
        self.assertEqual(status['state'], 'done')
        self.assertEqual(self.provenance()['0,0'], {'source': '3' * 64, 'pair': self.index_pairs()['0,0']})

    def test_missing_or_malformed_provenance_forces_a_rerender_never_done_by_assumption(self):
        self.export(['-1,-1', '0,0'])
        pair = self.index_pairs()['0,0']
        cases = [None, b'not json', json.dumps({'format': 'OTHER', 'tiles': {}}).encode(),
                 json.dumps({'format': ce.PROVENANCE_FORMAT, 'tiles': {'0,0': {'source': '2' * 64, 'pair': 'f' * 64}}}).encode(),
                 json.dumps({'format': ce.PROVENANCE_FORMAT, 'tiles': {'0,0': {'source': 'X', 'pair': pair}}}).encode()]
        for content in cases:
            with self.subTest(content=content):
                if content is None:
                    (self.out / 'provenance.json').unlink()
                else:
                    (self.out / 'provenance.json').write_bytes(content)
                renders = len(self.calls)
                status = self.export(['0,0'])
                self.assertEqual(len(self.calls), renders + 1, 'untrusted provenance never permits reuse')
                self.assertEqual(status['tiles']['0,0']['result'], 'published')
                self.assertEqual(self.index_pairs()['0,0'], pair, 'same inputs -> same pair')
                self.assertEqual(self.provenance()['0,0'], {'source': '2' * 64, 'pair': pair})
        self.assertIsNotNone(status['provenance']['ignored'])

    def test_unreadable_or_invalid_source_revision_fails_the_tile(self):
        self.export(['0,0'])
        before = self.pointer()
        for value, pattern in [(OSError('region header unreadable'), 'unreadable'), ('xyz', 'invalid source revision')]:
            self.revisions['0,0'] = value
            status = self.export(['0,0'])
            self.assertEqual(status['tiles']['0,0']['result'], 'failed')
            self.assertIn(pattern, status['tiles']['0,0']['reason'])
            self.assertEqual(status['state'], 'partial')
        self.assertEqual(self.calls, ['0,0'], 'no render without a trustworthy revision')
        self.assertEqual(self.pointer(), before)

    def test_legacy_callers_without_a_source_callback_do_not_touch_provenance(self):
        status = self.run_export(['0,0'])
        self.assertFalse(status['sourceRevisionChecked'])
        self.assertFalse((self.out / 'provenance.json').exists())

    def test_metadata_reserve_is_kept_in_every_budget_check(self):
        self.assertEqual(ce.metadata_reserve(0), 64 * 1024)
        self.assertEqual(ce.metadata_reserve(10) - ce.metadata_reserve(0), 10 * 2 * (ce.INDEX_ENTRY_BYTES + ce.PROVENANCE_ENTRY_BYTES))
        status = self.export(['0,0'], budget_bytes=ce.metadata_reserve(1) - 1, estimate_tile_bytes=0)
        self.assertTrue(status['state'].startswith('failed: budget'))
        self.assertIn('metadata reserve', status['state'])
        tile = self.tiles['0,0']
        class Render:
            reserve_bytes = 0
            def __call__(self, tx, tz):
                return tile
        render = Render()
        status = self.export(['0,0'], render=render)
        self.assertEqual(render.reserve_bytes, ce.metadata_reserve(1), 'the region copies keep the same headroom')
        self.assertEqual(status['state'], 'done')


class SourceRevisionTests(unittest.TestCase):
    def test_cli_revision_covers_the_tile_input_and_the_renderer_registry(self):
        areas = []
        fingerprints = {'value': 'f1'}
        def fingerprint(region_dir, area):
            areas.append((area.x, area.z, area.width, area.depth))
            return fingerprints['value']
        registry = {'1': 'minecraft:stone', 0: 'minecraft:air'}
        current = {'registry': dict(registry)}
        revision = ce.make_source_revision(Path('world'), ce.registry_sha256(registry), fingerprint=fingerprint,
                                           load_registry=lambda _w: current['registry'])
        first = revision(0, -1)
        self.assertRegex(first, r'^[0-9a-f]{64}$')
        self.assertEqual(areas[0], (-16, -80, 96, 96), 'the input window includes the 16 block halo')
        self.assertEqual(revision(0, -1), first, 'unchanged inputs -> same revision')
        self.assertNotEqual(revision(1, -1), first, 'bound to the tile')
        fingerprints['value'] = 'f2'                       # any saved chunk of the input (hidden blocks too)
        self.assertNotEqual(revision(0, -1), first)
        self.assertEqual(ce.registry_sha256({0: 'minecraft:air', 1: 'minecraft:stone'}), ce.registry_sha256(registry))
        current['registry'] = {**registry, 2: 'mod:new_block'}
        with self.assertRaisesRegex(ce.TileError, 'registry changed'):
            revision(0, -1)


class BoundedCopyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.regions, self.out = base / 'region', base / 'out'
        self.regions.mkdir(); self.out.mkdir()
        self.scratch = self.out / '.scratch-test'
        self.scratch.mkdir()
        # Tile 0,0 with its halo touches regions (-1,-1), (0,-1), (-1,0) and (0,0).
        for name, size in {'r.0.0.mca': 3000, 'r.-1.0.mca': 2000, 'r.5.5.mca': 10 ** 6}.items():
            (self.regions / name).write_bytes(bytes([7]) * size)
        self.area = wh._Rect(-16, -16, 96, 96)         # the input window (core + halo) of tile 0,0

    def copy(self, **kw):
        options = dict(out=self.out, budget_bytes=10 ** 6, min_free_bytes=0, disk_free=lambda _p: 10 ** 12,
                       sleep=lambda _s: None)
        options.update(kw)
        return ce.copy_regions_bounded(self.regions, self.scratch, self.area, **options)

    def test_copies_only_the_touching_regions_exactly(self):
        copies = self.copy()
        self.assertEqual(set(copies), {(0, 0), (-1, 0)})
        self.assertEqual(copies[(0, 0)].read_bytes(), (self.regions / 'r.0.0.mca').read_bytes())

    def test_refused_before_copying_when_sources_do_not_fit(self):
        (self.out / 'store.bin').write_bytes(b'x' * 1000)               # existing owned output counts
        with self.assertRaises(ce.CopyBudgetError):
            self.copy(budget_bytes=5500)                                # 1000 used + 5000 sources > 5500
        self.assertEqual(list(self.scratch.iterdir()), [], 'nothing copied')
        with self.assertRaises(ce.CopyBudgetError):
            self.copy(min_free_bytes=20 * 1024 ** 3, disk_free=lambda _p: 20 * 1024 ** 3 + 4000)
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual((self.out / 'store.bin').read_bytes(), b'x' * 1000, 'existing output is never deleted')

    def test_growth_is_detected_and_never_copied_unboundedly(self):
        real = os.stat
        def shrunk_stat(path, *a, **k):
            st = real(path, *a, **k)
            if Path(path).name == 'r.0.0.mca':                          # the file is larger than observed
                return SimpleNamespace(st_size=100, st_mtime_ns=st.st_mtime_ns)
            return st
        sizes = []
        target = self.scratch / 'r.0.0.mca'
        with self.assertRaisesRegex(ce.TileError, 'kept changing'):
            self.copy(stat=shrunk_stat, sleep=lambda _s: sizes.append(target.stat().st_size))
        self.assertEqual(sizes, [101, 101], 'each attempt stops at observed size + 1, retries reuse one target')
        self.assertFalse(target.exists())

    def test_changing_mtime_is_retried_then_accepted(self):
        real = os.stat
        calls = {'n': 0}
        def flapping_stat(path, *a, **k):
            st = real(path, *a, **k)
            if Path(path).name == 'r.0.0.mca':
                calls['n'] += 1
                if calls['n'] == 3:                                      # 1: total, 2: before, 3: after
                    return SimpleNamespace(st_size=st.st_size, st_mtime_ns=st.st_mtime_ns + 1)
            return st
        copies = self.copy(stat=flapping_stat)
        self.assertEqual(copies[(0, 0)].stat().st_size, 3000)
        self.assertGreaterEqual(calls['n'], 5)


class ContentDigestTests(unittest.TestCase):
    def test_same_header_and_mtime_payload_change_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            region = root / 'r.0.0.mca'
            raw = bytearray(4 * 4096)
            raw[0:4] = (2 * 256 + 1).to_bytes(4, 'big')
            raw[4:8] = (3 * 256 + 1).to_bytes(4, 'big')
            raw[8192:8199] = b'\x00\x00\x00\x03\x02ab'
            raw[12288:12295] = b'\x00\x00\x00\x03\x02cd'
            region.write_bytes(raw)
            stamp = region.stat()
            area = wh._Rect(0, 0, 16, 16)
            first = ce.content_fingerprint(root, area)
            raw[8198] = ord('z')
            region.write_bytes(raw)
            os.utime(region, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            changed = ce.content_fingerprint(root, area)
            self.assertNotEqual(first, changed)
            raw[12294] = ord('q')  # outside the requested input, so does not invalidate this tile
            region.write_bytes(raw)
            self.assertEqual(changed, ce.content_fingerprint(root, area))
            raw[8192:8196] = (999999).to_bytes(4, 'big')
            region.write_bytes(raw)
            with self.assertRaisesRegex(ce.TileError, 'payload length'):
                ce.content_fingerprint(root, area)


class FreeFloorTests(ExportHarness):
    def test_payload_cannot_consume_reserved_free_space(self):
        reserve = ce.metadata_reserve(1)
        status = self.run_export(['0,0'], min_free_bytes=1000,
                                 disk_free=lambda _p: 1000 + reserve + 100)
        self.assertEqual(status['state'], 'partial')
        self.assertTrue(status['budgetStopped'])
        self.assertEqual(status['published'], 0)
        self.assertFalse((self.out / 'store' / 'objects').exists())


class SelectionTests(unittest.TestCase):
    def test_selection_limits_and_tile_syntax(self):
        self.assertEqual(ce.select_tiles(['1,2', '1,2', '-3,4', '5,6'], 2), ([(1, 2), (-3, 4)], [(5, 6)]))
        self.assertEqual(ce.select_tiles(['0,0']), ([(0, 0)], []))
        for texts, max_tiles in [([], 4), (['0,0'] * 129, 4), (['0,0'], 0), (['0,0'], 33)]:
            with self.assertRaises(ce.ExportError):
                ce.select_tiles(texts, max_tiles)
        for text in ['1;2', '01,2', '-0,1', '468751,0', ' 1,2', '1,2,3']:
            with self.assertRaises(ce.ExportError):
                ce.select_tiles([text])

    def test_output_must_lie_outside_every_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            world, hires, catalog = base / 'world', base / 'hires', base / 'catalog'
            for p in (world, hires, catalog):
                p.mkdir()
            for out in (world, world / 'collision', hires / 'x', catalog):
                with self.assertRaises(ce.ExportError):
                    ce.check_paths(out, world=world, hires=hires, catalog=catalog)
            with self.assertRaises(ce.ExportError):
                ce.check_paths(base, world=world)               # the output would contain the world
            self.assertEqual(ce.check_paths(base / 'out', world=world, hires=hires, catalog=catalog),
                             (base / 'out').resolve())

    def test_cli_requires_explicit_selection_and_safe_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'world').mkdir()
            common = ['--world', str(base / 'world'), '--catalog-dir', str(base / 'c'), '--hires-store', str(base / 'h')]
            with self.assertRaises(SystemExit):
                ce.main(common + ['--out', str(base / 'out')])               # no --tile
            self.assertEqual(ce.main(common + ['--out', str(base / 'world' / 'o'), '--tile', '0,0']), 1)
            self.assertEqual(ce.main(common + ['--out', str(base / 'out'), '--tile', '0,0', '--max-tiles', '40']), 1)
            self.assertFalse((base / 'world' / 'o').exists() or (base / 'out').exists(), 'nothing created')


if __name__ == '__main__':
    unittest.main()

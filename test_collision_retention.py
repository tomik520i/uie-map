"""Tests for collision_retention.py (read-only retention planner) on a real small collision store built with
collision_bundle.publish_pair / publish_index. Stdlib only.
    python -m unittest test_collision_retention -v      (from work/uie-map)
"""
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

import collision_retention as cr
from collision_bundle import publish_index, publish_pair
import test_collision_tile as fixtures
from test_offline_renderer import volume
from test_world_hires import quad

OLD = 10_000          # seconds in the past for "old" files (grace is 1800)


class StoreHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name).resolve() / 'store'
        self.store.mkdir()
        tile_a = fixtures.CollisionTileTests().fixture()                          # tile -1,-1
        mesh_a = {'chunks': {(-4, -4): quad(-63, 64, -63)}}
        self.a = publish_pair(self.store, tile_a, mesh_a, 'a' * 64)
        self.a2 = publish_pair(self.store, tile_a, mesh_a, 'b' * 64)             # same objects, other pair
        self.b = publish_pair(self.store, volume({(1, 64, 1): (1, 0)}, x0=0, z0=0, size=64, core=(0, 0, 64, 64)),
                              {'chunks': {(0, 0): quad(1, 64, 1)}}, 'a' * 64)      # tile 0,0
        self.c = publish_pair(self.store, volume({}, x0=64, z0=0, size=64, core=(64, 0, 64, 64)),
                              {'chunks': {}}, 'a' * 64)                            # tile 1,0, no geometry
        self.previous = publish_index(self.store, {'-1,-1': self.a2['pairSha256'], '0,0': self.b['pairSha256']})
        self.current = publish_index(self.store, {'-1,-1': self.a['pairSha256'], '1,0': self.c['pairSha256']})
        self.now = time.time()
        for path in self.files():
            self.age(path, OLD)

    def files(self):
        return [p for p in self.store.rglob('*') if p.is_file()]

    def age(self, path, seconds):
        stamp = self.now - seconds
        os.utime(path, (stamp, stamp))

    def snapshot(self):
        return {p.relative_to(self.store).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.files()}

    def plan(self, **kw):
        options = dict(now=self.now)
        options.update(kw)
        return cr.plan_retention(self.store, **options)

    @staticmethod
    def paths(plan):
        return [c['path'] for c in plan['candidates']]

    def obj(self, pair, kind):
        sha = pair['blocksSha256'] if kind == 'uib' else pair['geometrySha256']
        return f'objects/{sha}.{kind}'


class PlanTests(StoreHarness):
    def test_empty_retained_blocks_refuse_even_without_full_object_verification(self):
        (self.store / self.obj(self.a, 'uib')).write_bytes(b'')
        with self.assertRaises(cr.RetentionRefused):
            self.plan()

    def test_nonfinite_clock_or_grace_refused(self):
        for value in [float('nan'), float('inf')]:
            with self.assertRaises(ValueError): self.plan(now=value)
            with self.assertRaises(ValueError): self.plan(grace_seconds=value)

    def test_current_closure_kept_old_orphans_are_candidates_and_shared_objects_stay(self):
        before = self.snapshot()
        plan = self.plan()
        self.assertEqual(plan['currentIndex'], self.current['indexSha256'])
        self.assertEqual(sorted(self.paths(plan)), sorted([
            f'indexes/{self.previous["indexSha256"]}.json', f'pairs/{self.a2["pairSha256"]}.json',
            f'pairs/{self.b["pairSha256"]}.json', self.obj(self.b, 'uib'), self.obj(self.b, 'uih')]))
        for kept in (f'indexes/{self.current["indexSha256"]}.json', f'pairs/{self.a["pairSha256"]}.json',
                     f'pairs/{self.c["pairSha256"]}.json', self.obj(self.a, 'uib'), self.obj(self.a, 'uih'),
                     self.obj(self.c, 'uib')):
            self.assertIn(kept, plan['keep'])
        self.assertNotIn(self.obj(self.a2, 'uib'), self.paths(plan), 'objects shared with a kept pair stay')
        candidate = plan['candidates'][0]
        self.assertEqual(set(candidate), {'path', 'size', 'mtimeNs', 'ino', 'dev'})
        info = os.stat(self.store / candidate['path'])
        self.assertEqual((candidate['size'], candidate['mtimeNs'], candidate['ino']),
                         (info.st_size, info.st_mtime_ns, info.st_ino))
        self.assertEqual(self.paths(plan), sorted(self.paths(plan)), 'sorted relative paths')
        self.assertEqual(plan['candidateBytes'], sum(c['size'] for c in plan['candidates']))
        self.assertEqual(self.snapshot(), before, 'the planner never changes a byte or an mtime')

    def test_explicit_previous_index_keeps_its_closure(self):
        plan = self.plan(previous_index=self.previous['indexSha256'])
        self.assertEqual(plan['candidates'], [])
        self.assertIn(f'pairs/{self.b["pairSha256"]}.json', plan['keep'])
        self.assertEqual(plan['retained']['indexes'], 2)

    def test_young_index_pair_and_object_are_protected_with_their_closure(self):
        self.age(self.store / 'indexes' / f'{self.previous["indexSha256"]}.json', 60)
        self.assertEqual(self.plan()['candidates'], [], 'a young index keeps its whole closure')
        self.setUp()
        self.age(self.store / 'pairs' / f'{self.b["pairSha256"]}.json', -300)        # future mtime: clock skew
        orphan_old, orphan_young = 'f' * 64 + '.uib', 'e' * 64 + '.uih'
        for name, seconds in ((orphan_old, OLD), (orphan_young, 5)):
            (self.store / 'objects' / name).write_bytes(b'orphan')
            self.age(self.store / 'objects' / name, seconds)
        plan = self.plan()
        self.assertIn(f'pairs/{self.b["pairSha256"]}.json', plan['keep'])
        self.assertIn(self.obj(self.b, 'uih'), plan['keep'], 'objects of a young pair stay')
        self.assertIn(f'objects/{orphan_young}', plan['keep'])
        self.assertIn(f'objects/{orphan_old}', self.paths(plan))
        self.assertIn(f'pairs/{self.a2["pairSha256"]}.json', self.paths(plan))

    def test_unknown_files_are_counted_never_candidates(self):
        (self.store / 'notes.txt').write_text('operator notes')
        (self.store / 'objects' / '.object-abc123').write_bytes(b'temp')
        (self.store / 'pairs' / 'nested').mkdir()
        plan = self.plan()
        self.assertEqual(plan['unknown']['count'], 3)
        self.assertEqual(plan['unknown']['examples'], ['notes.txt', 'objects/.object-abc123', 'pairs/nested'])
        self.assertFalse(any('notes' in p or '.object-' in p or 'nested' in p for p in self.paths(plan)))

    def test_damaged_retained_graph_refuses_the_whole_plan(self):
        cases = {
            'corrupt current index': lambda: (self.store / 'indexes' / f'{self.current["indexSha256"]}.json')
                .write_bytes(b'{"format":"UIE_COLLISION_INDEX1","tileSize":64,"pairs":{}}'),
            'missing object of a current pair': lambda: (self.store / self.obj(self.a, 'uib')).unlink(),
            'empty geometry of a current pair': lambda: (self.store / self.obj(self.a, 'uih')).write_bytes(b''),
            'missing current pair': lambda: (self.store / 'pairs' / f'{self.c["pairSha256"]}.json').unlink(),
            'corrupt young pair': lambda: self._young_garbage_pair(),
            'invalid pointer': lambda: (self.store / 'current.json').write_text('{"format":"x"}'),
            'missing pointer': lambda: (self.store / 'current.json').unlink(),
        }
        for label, damage in cases.items():
            with self.subTest(label):
                self.setUp()
                damage()
                with self.assertRaises(cr.RetentionRefused):
                    self.plan()
        self.setUp()
        with self.assertRaisesRegex(cr.RetentionRefused, 'previous index'):
            self.plan(previous_index='d' * 64)

    def _young_garbage_pair(self):
        path = self.store / 'pairs' / ('9' * 64 + '.json')
        path.write_text('{"not":"a pair"}')
        self.age(path, 1)

    def test_symlinks_are_refused(self):
        target = self.store / self.obj(self.a, 'uib')
        link = self.store / 'objects' / ('8' * 64 + '.uib')
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are not available here')
        with self.assertRaisesRegex(cr.RetentionRefused, 'symlink'):
            self.plan()

    def test_pointer_change_during_planning_is_refused(self):
        real = cr._read_pointer
        calls = []

        def changing(store):
            calls.append(1)
            digest, index = real(store)
            return (digest if len(calls) == 1 else '0' * 64), index
        with patch.object(cr, '_read_pointer', side_effect=changing):
            with self.assertRaisesRegex(cr.RetentionRefused, 'changed during planning'):
                self.plan()

    def test_object_verification_is_optional_and_strict(self):
        self.assertTrue(self.plan(verify_objects=True)['objectsVerified'])
        target = self.store / self.obj(self.a, 'uih')
        target.write_bytes(b'x' * target.stat().st_size)               # same size, wrong content
        self.age(target, OLD)
        self.plan()                                                     # existence/size check only
        with self.assertRaisesRegex(cr.RetentionRefused, 'failed verification'):
            self.plan(verify_objects=True)

    def test_inputs_and_bounds(self):
        with self.assertRaises(ValueError):
            self.plan(grace_seconds=1799)
        with self.assertRaises(ValueError):
            self.plan(previous_index='nope')
        with self.assertRaisesRegex(cr.RetentionRefused, 'absolute'):
            cr.plan_retention(Path('relative-store'), now=self.now)
        with patch.object(cr, 'MAX_FILES', 3):
            with self.assertRaisesRegex(cr.RetentionRefused, 'files'):
                self.plan()
        with patch.object(cr, 'MAX_METADATA_BYTES', 100):
            with self.assertRaisesRegex(cr.RetentionRefused, 'metadata'):
                self.plan()

    def test_module_never_mutates_files(self):
        source = Path(cr.__file__).read_text(encoding='utf-8')
        self.assertIsNone(re.search(r'\b(unlink|rmtree|remove|removedirs|rmdir|rename|replace|truncate|utime|chmod|'
                                    r'write_text|write_bytes|mkdir|makedirs|symlink|link)\s*\(', source))
        self.assertIsNone(re.search(r"open\([^)]*['\"][wax]", source), 'files are only opened for reading')


class SelectActivePairsTests(unittest.TestCase):
    PAIRS = {f'{i},0': f'{i:x}' * 64 for i in range(1, 7)}

    def test_pins_first_then_recent_capped_and_deterministic(self):
        result = cr.select_active_pairs(self.PAIRS, ['2,0', '1,0'], ['5,0', '1,0', '9,0', '3,0', '4,0'], max_tiles=4)
        self.assertEqual(result['kept'], ['2,0', '1,0', '5,0', '3,0'])
        self.assertEqual(list(result['pairs']), result['kept'])
        self.assertEqual(result['dropped'], ['4,0', '6,0'])
        self.assertEqual((result['missingPinned'], result['missingRecent']), ([], ['9,0']))
        self.assertEqual(cr.select_active_pairs(self.PAIRS, ['2,0', '1,0'], ['5,0', '1,0', '9,0', '3,0', '4,0'],
                                                max_tiles=4), result)

    def test_missing_pins_are_reported_not_invented_and_pins_must_fit(self):
        result = cr.select_active_pairs(self.PAIRS, ['7,0', '1,0'], [], max_tiles=32)
        self.assertEqual((result['kept'], result['missingPinned']), (['1,0'], ['7,0']))
        self.assertNotIn('7,0', result['pairs'])
        with self.assertRaises(ValueError):
            cr.select_active_pairs(self.PAIRS, ['1,0', '2,0', '3,0'], [], max_tiles=2)
        with self.assertRaises(ValueError):
            cr.select_active_pairs(self.PAIRS, ['01,0'], [])
        with self.assertRaises(ValueError):
            cr.select_active_pairs({'1,0': 'short'}, [], [])


if __name__ == '__main__':
    unittest.main()

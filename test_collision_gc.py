"""Tests for collision_gc.py (private GC executor) on a temporary owned collision output built with
collision_bundle.publish_pair / publish_index. Never touches any real store. Stdlib only.
    python -m unittest test_collision_gc -v      (from work/uie-map)
"""
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

import collision_gc as cg
import collision_bundle as cb
import collision_retention as cr
from collision_bundle import publish_index, publish_pair
import test_collision_tile as fixtures
from test_offline_renderer import volume
from test_world_hires import quad

OLD = 10_000
GRACE = cg.MIN_UNREFERENCED_SECONDS


class Handle:
    def __init__(self, owner):
        self.owner = owner

    def close(self):
        self.owner.locks_closed += 1


class Crash(Exception):
    """Simulated process death in the middle of a deletion phase."""


class GcHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name).resolve() / 'out'
        self.store = self.out / 'store'
        self.store.mkdir(parents=True)
        (self.out / cg.OWNED_MARKER).write_text('owned by the collision exporter (test)')
        tile_a = fixtures.CollisionTileTests().fixture()
        mesh_a = {'chunks': {(-4, -4): quad(-63, 64, -63)}}
        self.a = publish_pair(self.store, tile_a, mesh_a, 'a' * 64)
        self.a2 = publish_pair(self.store, tile_a, mesh_a, 'b' * 64)             # shares a's objects
        self.b = publish_pair(self.store, volume({(1, 64, 1): (1, 0)}, x0=0, z0=0, size=64, core=(0, 0, 64, 64)),
                              {'chunks': {(0, 0): quad(1, 64, 1)}}, 'a' * 64)
        self.c = publish_pair(self.store, volume({}, x0=64, z0=0, size=64, core=(64, 0, 64, 64)),
                              {'chunks': {}}, 'a' * 64)
        self.previous = publish_index(self.store, {'-1,-1': self.a2['pairSha256'], '0,0': self.b['pairSha256']})
        self.current = publish_index(self.store, {'-1,-1': self.a['pairSha256'], '1,0': self.c['pairSha256']})
        self.now = time.time()
        for path in self.store.rglob('*'):
            if path.is_file():
                os.utime(path, (self.now - OLD,) * 2)            # ancient, like a long-lived store
        cg.enable_gc(self.out, now=self.now - OLD)                # deliberate opt-in, long ago (nothing protected)
        self.busy = False
        self.locks_closed = 0

    def journal(self):
        return json.loads((self.out / cg.JOURNAL_NAME).read_text())['indexes']

    def lock(self, _out):
        return None if self.busy else Handle(self)

    def run_gc(self, at=0, **kw):
        return cg.run_gc(self.out, lock=self.lock, now=self.now + at, **kw)

    def rel(self, kind, item):
        if kind == 'index':
            return f'indexes/{item["indexSha256"]}.json'
        if kind == 'pair':
            return f'pairs/{item["pairSha256"]}.json'
        return f'objects/{item["blocksSha256"]}.uib' if kind == 'uib' else f'objects/{item["geometrySha256"]}.uih'

    def stale_set(self):
        return sorted([self.rel('index', self.previous), self.rel('pair', self.a2), self.rel('pair', self.b),
                       self.rel('uib', self.b), self.rel('uih', self.b)])

    def ledger(self):
        return json.loads((self.out / cg.STATE_NAME).read_text())['firstSeenUnreferenced']

    def exists(self, rel):
        return (self.store / rel).exists()

    def assert_graph_consistent(self):
        """Every remaining index refers only to existing pairs, every remaining pair only to existing objects,
        and the current closure still plans cleanly."""
        reader = cr._Reader(self.store)
        for index in (self.store / 'indexes').iterdir():
            for pair in cr._index_pairs(reader, index.name[:-5]).values():
                self.assertTrue(self.exists(f'pairs/{pair}.json'), f'{index.name} -> missing pair {pair}')
        for pair in (self.store / 'pairs').iterdir():
            for name, *_ in cr._pair_objects(reader, pair.name[:-5]):
                self.assertTrue(self.exists(f'objects/{name}'), f'{pair.name} -> missing object {name}')
        cr.plan_retention(self.store, now=self.now, verify_objects=True)


class GcTests(GcHarness):
    def test_first_observation_never_deletes_then_grace_then_safe_order(self):
        first = self.run_gc(0)
        self.assertEqual((first['state'], first['deleted'], first['candidates'], first['newlyObserved']),
                         ('done', [], 5, 5), 'an ancient but just-unreferenced index is never deleted at once')
        self.assertEqual(sorted(self.ledger()), self.stale_set())
        self.assertEqual(self.run_gc(GRACE - 1)['deleted'], [])
        done = self.run_gc(GRACE)
        self.assertEqual(sorted(done['deleted']), self.stale_set())
        self.assertEqual([p.split('/')[0] for p in done['deleted']], ['indexes', 'pairs', 'pairs', 'objects', 'objects'])
        self.assertTrue(self.exists(self.rel('uib', self.a)), 'objects shared with the current pair stay')
        self.assertEqual(self.ledger(), {})
        self.assertEqual(self.locks_closed, 3)
        self.assert_graph_consistent()
        self.assertEqual(cr.plan_retention(self.store, now=self.now)['candidates'], [])

    def test_recently_retired_ancient_current_index_is_protected_by_the_journal(self):
        newest = publish_index(self.store, {'-1,-1': self.a['pairSha256'], '1,0': self.c['pairSha256'],
                                            '0,0': self.b['pairSha256']}, now=self.now + 5)
        self.assertNotEqual(newest['indexSha256'], self.current['indexSha256'])
        self.assertEqual(self.journal()[self.current['indexSha256']], self.now + 5, 'outgoing index journaled')
        old_current = self.rel('index', self.current)
        self.assertNotIn(old_current, self.ledger() if (self.out / cg.STATE_NAME).exists() else {})
        self.run_gc(0)
        self.assertNotIn(old_current, self.ledger(), 'old mtime, but published 5 s ago: shielded')
        self.run_gc(GRACE + 4)
        self.assertNotIn(old_current, self.ledger(), 'still within the protection period')
        self.run_gc(GRACE + 5)
        self.assertEqual(self.ledger()[old_current]['firstSeen'], self.now + GRACE + 5, 'then a fresh observation')
        self.assertNotIn(old_current, self.run_gc(2 * GRACE + 4)['deleted'])
        self.assertIn(old_current, self.run_gc(2 * GRACE + 5)['deleted'])
        self.assert_graph_consistent()

    def test_previous_and_young_closures_are_never_candidates(self):
        self.assertEqual(self.run_gc(GRACE * 10, previous_index=self.previous['indexSha256'])['candidates'], 0)
        os.utime(self.store / self.rel('index', self.previous), (self.now - 60,) * 2)
        self.assertEqual(self.run_gc(0)['candidates'], 0, 'a young index keeps its closure (planner)')

    def test_reference_again_resets_the_ledger(self):
        self.run_gc(0)
        publish_index(self.store, {'-1,-1': self.a['pairSha256'], '1,0': self.c['pairSha256'], '0,0': self.b['pairSha256']},
                      now=self.now + 1)
        middle = self.run_gc(GRACE)
        self.assertNotIn(self.rel('pair', self.b), self.ledger(), 'referenced again: removed from the ledger')
        self.assertTrue(self.exists(self.rel('pair', self.b)))
        self.assertIn(self.rel('index', self.previous), middle['deleted'])
        publish_index(self.store, {'-1,-1': self.a['pairSha256'], '1,0': self.c['pairSha256']},
                      now=self.now + 2)                          # back to current
        later = 2 * GRACE + 2                                    # intermediate index: not young, not journaled
        self.run_gc(later)
        self.assertEqual(self.ledger()[self.rel('pair', self.b)]['firstSeen'], self.now + later, 'starts over')
        self.assertTrue(self.exists(self.rel('pair', self.b)))
        self.assertNotIn(self.rel('pair', self.b), self.run_gc(later + GRACE - 1)['deleted'])
        self.assertIn(self.rel('pair', self.b), self.run_gc(later + GRACE)['deleted'])
        self.assert_graph_consistent()

    def test_busy_lock_reads_and_writes_nothing(self):
        self.busy = True
        result = self.run_gc(GRACE * 10)
        self.assertEqual(result['state'], 'busy')
        self.assertFalse((self.out / cg.STATE_NAME).exists())
        self.assertTrue(all(self.exists(rel) for rel in self.stale_set()))

    def test_missing_ledger_is_fresh_corrupt_or_foreign_ledger_refuses(self):
        self.assertEqual(self.run_gc(GRACE * 10)['deleted'], [], 'missing ledger: first observation only')
        for content in ('{broken', json.dumps({'format': cg.STATE_FORMAT, 'store': '/elsewhere',
                                               'firstSeenUnreferenced': {}}),
                        json.dumps({'format': cg.STATE_FORMAT, 'store': str(self.store),
                                    'firstSeenUnreferenced': {'../escape.json': {}}})):
            (self.out / cg.STATE_NAME).write_text(content)
            result = self.run_gc(GRACE * 20)
            self.assertEqual((result['state'], result.get('deleted')), ('refused', []))
            self.assertEqual((self.out / cg.STATE_NAME).read_text(), content, 'a refused ledger is not rewritten')
        self.assertTrue(all(self.exists(rel) for rel in self.stale_set()))

    def test_future_first_seen_never_ages_a_file(self):
        plan = cr.plan_retention(self.store, now=self.now)
        entries = {c['path']: {'firstSeen': self.now + 50_000, **{k: c[k] for k in ('size', 'mtimeNs', 'ino', 'dev')}}
                   for c in plan['candidates']}
        cg.write_ledger(self.out / cg.STATE_NAME, self.store, entries, self.now, plan['pointerSha256'])
        self.assertEqual(self.run_gc(GRACE)['deleted'], [])
        self.assertEqual(self.run_gc(GRACE * 2 - 1)['deleted'], [], 'clamped to the observation time')

    def test_ownership_and_symlinks(self):
        (self.out / cg.OWNED_MARKER).unlink()
        self.assertEqual(self.run_gc(0)['state'], 'refused')
        (self.out / cg.OWNED_MARKER).mkdir()
        self.assertEqual(self.run_gc(0)['state'], 'refused', 'the marker must be a regular file')
        (self.out / cg.OWNED_MARKER).rmdir()
        (self.out / cg.OWNED_MARKER).write_text('owned')
        try:
            os.symlink(self.store / self.rel('uib', self.a), self.store / 'objects' / ('7' * 64 + '.uib'))
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are not available here')
        result = self.run_gc(0)
        self.assertEqual(result['state'], 'refused')
        self.assertIn('symlink', result['reason'])

    def test_pointer_race_stops_before_any_unlink(self):
        self.run_gc(0)
        real, calls = cr._read_pointer, []

        def racing(store):
            calls.append(1)
            digest, index = real(store)
            return (digest if len(calls) <= 2 else '0' * 64), index     # changes after the plan's two reads
        with patch.object(cr, '_read_pointer', side_effect=racing):
            result = self.run_gc(GRACE)
        self.assertEqual((result['state'], result['deleted']), ('stopped', []))
        self.assertIn('current.json changed', result['reason'])
        self.assertTrue(all(self.exists(rel) for rel in self.stale_set()))

    def test_stat_race_stops_before_unlink(self):
        self.run_gc(0)
        real = cr.plan_retention
        target = self.store / self.rel('index', self.previous)

        def plan_then_touch(*args, **kw):
            plan = real(*args, **kw)
            os.utime(target, (self.now - OLD + 1,) * 2)          # another writer touched it after the plan
            return plan
        with patch.object(cr, 'plan_retention', side_effect=plan_then_touch):
            result = self.run_gc(GRACE)
        self.assertEqual((result['state'], result['deleted']), ('stopped', []))
        self.assertIn('changed after planning', result['reason'])
        self.assertTrue(target.exists())

    def test_file_and_byte_caps(self):
        self.run_gc(0)
        capped = self.run_gc(GRACE, max_delete_files=2)
        self.assertEqual(len(capped['deleted']), 2)
        self.assertEqual(capped['skipped']['fileBudget'], 3)
        self.assertEqual(capped['deleted'][0], self.rel('index', self.previous), 'indexes go first')
        self.assert_graph_consistent()
        self.setUp()
        self.run_gc(0)
        index_size = (self.store / self.rel('index', self.previous)).stat().st_size
        small = self.run_gc(GRACE, max_delete_bytes=index_size)
        self.assertEqual(small['deleted'], [self.rel('index', self.previous)])
        self.assertGreater(small['skipped']['byteBudget'], 0)
        with self.assertRaises(ValueError):
            self.run_gc(0, max_delete_files=0)
        with self.assertRaises(ValueError):
            self.run_gc(0, min_unreferenced_seconds=GRACE - 1)

    def test_pairs_and_objects_of_a_remaining_index_are_kept(self):
        plan = cr.plan_retention(self.store, now=self.now)
        entries = {}
        for c in plan['candidates']:
            mature = c['path'] != self.rel('index', self.previous)          # the index itself is still fresh
            entries[c['path']] = {'firstSeen': self.now - (GRACE + 100 if mature else 0),
                                  **{k: c[k] for k in ('size', 'mtimeNs', 'ino', 'dev')}}
        cg.write_ledger(self.out / cg.STATE_NAME, self.store, entries, self.now, plan['pointerSha256'])
        result = self.run_gc(0)
        self.assertEqual(result['deleted'], [], 'the remaining old index still refers to them')
        self.assertEqual(result['skipped']['referencedByRemaining'], 4)
        self.assert_graph_consistent()

    def test_crash_mid_run_never_breaks_the_remaining_graph(self):
        self.run_gc(0)
        deleted = []

        def crashing_unlink(path):
            os.unlink(path)
            deleted.append(path)
            if len(deleted) == 2:
                raise Crash('process killed')
        with self.assertRaises(Crash):
            self.run_gc(GRACE, unlink=crashing_unlink)
        self.assertEqual(self.locks_closed, 2, 'the lock is released even on a crash')
        self.assertFalse(self.exists(self.rel('index', self.previous)), 'the index went first')
        self.assert_graph_consistent()
        gone = {Path(p).relative_to(self.store).as_posix() for p in deleted}
        recovery = self.run_gc(GRACE + 1)
        self.assertEqual(recovery['state'], 'done')
        self.assertEqual(sorted(recovery['deleted']), sorted(set(self.stale_set()) - gone),
                         'the next run finishes the remaining, still continuously unreferenced files')
        self.assert_graph_consistent()

    def test_ledger_lives_outside_the_store_and_the_module_never_removes_directories(self):
        self.run_gc(0)
        self.assertEqual((self.out / cg.STATE_NAME).parent, self.out)
        self.assertFalse((self.store / cg.STATE_NAME).exists())
        source = Path(cg.__file__).read_text(encoding='utf-8')
        self.assertIsNone(re.search(r'\b(rmtree|rmdir|removedirs|shutil)\b', source))
        self.assertIsNone(re.search(r'\brename\s*\(', source))

    def test_default_lock_is_exclusive_or_refuses(self):
        try:
            import fcntl
        except ImportError:
            result = cg.run_gc(self.out, now=self.now)
            self.assertEqual(result['state'], 'refused')
            self.assertIn('no exclusive file lock', result['reason'])
            return
        with open(self.out / cg.LOCK_NAME, 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(cg.run_gc(self.out, now=self.now)['state'], 'busy')


class PublicationJournalTests(GcHarness):
    """Task 118: B -> A -> B between two GC runs must give A a full grace since it was last current."""

    def publish_a(self, at):
        return publish_index(self.store, {'-1,-1': self.a2['pairSha256'], '0,0': self.b['pairSha256']}, now=self.now + at)

    def publish_b(self, at):
        return publish_index(self.store, {'-1,-1': self.a['pairSha256'], '1,0': self.c['pairSha256']}, now=self.now + at)

    def test_b_a_b_between_gc_runs_protects_a_for_a_full_grace(self):
        index_a = self.rel('index', self.previous)
        self.run_gc(0)                                            # A first seen unreferenced at T
        self.assertIn(index_a, self.ledger())
        self.assertEqual(self.publish_a(10)['indexSha256'], self.previous['indexSha256'])   # A current again
        self.publish_b(20)                                        # ... and replaced again, no GC in between
        protected = self.run_gc(GRACE)                            # T + 1800: the old ledger age must not count
        self.assertEqual(protected['deleted'], [], 'A was current 1780 s ago')
        self.assertTrue(self.exists(index_a) and self.exists(self.rel('pair', self.b)))
        self.assertGreaterEqual(protected['journalProtected'], 2)
        self.assertNotIn(index_a, self.ledger(), 'shielded: the old observation is dropped')
        self.assertEqual(self.run_gc(GRACE + 19)['deleted'], [])
        self.run_gc(GRACE + 20)                                   # protection over: a fresh observation starts
        self.assertEqual(self.ledger()[index_a]['firstSeen'], self.now + GRACE + 20)
        self.assertEqual(self.run_gc(2 * GRACE + 19)['deleted'], [])
        final = self.run_gc(2 * GRACE + 20)
        self.assertEqual(sorted(final['deleted']), self.stale_set(), 'eventually collected after the grace')
        self.assertNotIn(self.previous['indexSha256'], self.journal(), 'expired and gone: pruned')
        self.assertIn(self.current['indexSha256'], self.journal(), 'the current index entry stays')
        self.assert_graph_consistent()

    def test_journal_failure_refuses_the_publication_and_keeps_the_old_pointer(self):
        pointer = (self.store / 'current.json').read_bytes()
        with patch.object(cg, 'write_journal', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(ValueError, 'publication refused'):
                self.publish_a(10)
        self.assertEqual((self.store / 'current.json').read_bytes(), pointer)
        self.assertNotIn(self.previous['indexSha256'], {k for k, v in self.journal().items() if v > self.now})

    def test_journal_directory_sync_failure_keeps_the_old_pointer(self):
        pointer = (self.store / 'current.json').read_bytes()
        with patch.object(cb, '_fsync_directory', side_effect=OSError('directory sync failed')) as sync:
            with self.assertRaisesRegex(ValueError, 'publication refused'):
                self.publish_a(10)
        sync.assert_called_once_with(self.out)
        self.assertEqual((self.store / 'current.json').read_bytes(), pointer)

    def test_enabled_publication_syncs_journal_directory_before_pointer_directory(self):
        synced = []
        with patch.object(cb, '_fsync_directory', side_effect=lambda path: synced.append(path)):
            self.publish_a(10)
        self.assertEqual(synced, [self.out, self.store])

    def test_corrupt_object_in_journal_protected_old_closure_refuses_gc(self):
        self.publish_a(10)
        self.publish_b(20)
        object_path = self.store / self.rel('uib', self.b)
        object_path.write_bytes(b'corrupt block object')
        os.utime(object_path, (self.now - OLD,) * 2)
        result = self.run_gc(30)
        self.assertEqual((result['state'], result['deleted']), ('refused', []))
        self.assertIn('failed verification', result['reason'])
        self.assertIn(object_path.name, result['reason'])

    def test_enabled_output_without_a_valid_journal_fails_closed(self):
        pointer = (self.store / 'current.json').read_bytes()
        for damage in ('missing', 'corrupt', 'foreign'):
            with self.subTest(damage):
                self.setUp()
                path = self.out / cg.JOURNAL_NAME
                if damage == 'missing':
                    path.unlink()
                elif damage == 'corrupt':
                    path.write_text('{broken')
                else:
                    path.write_text(json.dumps({'format': cg.JOURNAL_FORMAT, 'store': '/elsewhere',
                                                'updatedAt': 0, 'indexes': {}}))
                pointer = (self.store / 'current.json').read_bytes()
                with self.assertRaises(ValueError):
                    self.publish_a(10)
                self.assertEqual((self.store / 'current.json').read_bytes(), pointer)
                self.assertEqual(self.run_gc(GRACE * 10)['state'], 'refused')
                self.assertTrue(all(self.exists(rel) for rel in self.stale_set()))

    def test_gc_requires_the_enabled_marker(self):
        (self.out / cg.ENABLED_MARKER).unlink()
        result = self.run_gc(GRACE * 10)
        self.assertEqual(result['state'], 'refused')
        self.assertIn('not enabled', result['reason'])

    def test_recent_journal_entry_for_a_missing_index_refuses(self):
        journal = cg.load_journal(self.out, self.store)
        journal['indexes']['d' * 64] = self.now
        cg.write_journal(self.out, self.store, journal)
        result = self.run_gc(0)
        self.assertEqual(result['state'], 'refused')
        self.assertIn('recently published index', result['reason'])

    def test_journal_time_never_goes_backwards_and_the_cap_refuses(self):
        self.publish_a(100)
        self.publish_b(50)                                        # clock went back 50 s
        journal = cg.load_journal(self.out, self.store)
        self.assertEqual(journal['indexes'][self.current['indexSha256']], self.now + 100, 'max(now, updatedAt)')
        self.assertEqual(journal['updatedAt'], self.now + 100)
        with patch.object(cg, 'MAX_JOURNAL_ENTRIES', 2):
            pointer = (self.store / 'current.json').read_bytes()
            with self.assertRaises(ValueError):
                publish_index(self.store, {'0,0': self.b['pairSha256']}, now=self.now + 200)
            self.assertEqual((self.store / 'current.json').read_bytes(), pointer, 'refused instead of dropping')

    def test_disabled_output_publication_is_unchanged_and_never_imports_gc(self):
        import sys
        plain = Path(self._tmp.name).resolve() / 'plain' / 'store'
        plain.mkdir(parents=True)
        pair = publish_pair(plain, volume({(1, 64, 1): (1, 0)}, x0=0, z0=0, size=64, core=(0, 0, 64, 64)),
                            {'chunks': {(0, 0): quad(1, 64, 1)}}, 'a' * 64)
        with patch.dict(sys.modules, {'collision_gc': None}), patch.object(cb, '_fsync_directory') as sync:
            result = publish_index(plain, {'0,0': pair['pairSha256']})
        sync.assert_not_called()
        self.assertEqual(json.loads((plain / 'current.json').read_text())['indexSha256'], result['indexSha256'])
        self.assertFalse((plain.parent / cg.JOURNAL_NAME).exists())

    def test_enable_gc_is_deliberate_and_protects_the_current_index(self):
        fresh = Path(self._tmp.name).resolve() / 'fresh'
        (fresh / 'store').mkdir(parents=True)
        with self.assertRaises(cg.GcRefused):
            cg.enable_gc(fresh)                                   # no ownership marker
        (fresh / cg.OWNED_MARKER).write_text('owned')
        journal = cg.enable_gc(fresh, now=self.now)
        self.assertEqual(journal['indexes'], {}, 'no pointer yet')
        self.assertTrue((fresh / cg.ENABLED_MARKER).is_file())
        self.assertEqual(cg.enable_gc(fresh, now=self.now + 1)['updatedAt'], self.now, 'idempotent')


if __name__ == '__main__':
    unittest.main()

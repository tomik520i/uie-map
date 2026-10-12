"""Tests of collision_world_store (POSIX: ctime proofs). Run: python3 -m unittest test_collision_world_store -v

RealCodecTests / WarmupAndCasTests / GcHookTests use REAL objects: UIB1 from collision_tile.encode_raw (on a small
stand-in Volume) + gzip, UIH1 from world_hires.encode_tile, canonical UIE_COLLISION_PAIR1 records, and the real
collision_bundle._verify_pair (only wrapped to count calls). LimitTests are SYNTHETIC: payload verification is
replaced by a stub so that 56 782-entry indexes stay fast; they test only counting, size and schema limits.
"""
from array import array
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import collision_bundle as cb
import collision_tile as ct
import collision_world_store as cws
import world_hires as wh

ATLAS = 'a' * 64


class FakeVolume:
    """Minimal input for collision_tile.encode_raw: one 64 x 64 core, every chunk present."""
    registry = {0: 'minecraft:air', 1: 'minecraft:stone'}

    def __init__(self, x0, z0, height=64):
        self.x0, self.z0 = x0, z0
        self.present = {(x0 // 16 + cx, z0 // 16 + cz) for cx in range(4) for cz in range(4)}
        self.ids = array('H', [1] * height + [0] * (256 - height)) * 4096
        self.metas = bytearray(4096 * 256)

    def column_index(self, x, z):
        dx, dz = x - self.x0, z - self.z0
        return dz * 64 + dx if 0 <= dx < 64 and 0 <= dz < 64 else -1


def quad(x, z):
    return {'positions': [x, 64, z, x + 1, 64, z, x + 1, 64, z + 1, x, 64, z + 1],
            'uvs': [0, 0, 1, 0, 1, 1, 0, 1], 'colors': [1.0, 1.0, 1.0] * 4}


def make_pair(store: Path, tx: int, tz: int, *, height=64, geometry=True):
    """Real pair objects, written exactly like collision_bundle.publish_pair. Returns (pairSha, blocksSha, geomSha)."""
    x, z = tx * 64, tz * 64
    raw = ct.encode_raw(FakeVolume(x, z, height), x, z)
    blocks_sha = ct.identity(raw)
    cb._immutable(store / 'objects' / f'{blocks_sha}.uib', ct.compressed(raw))
    geometry_sha = None
    if geometry:
        data = wh.encode_tile(x, z, [quad(x, z)])
        geometry_sha = hashlib.sha256(data).hexdigest()
        cb._immutable(store / 'objects' / f'{geometry_sha}.uih', data)
    record = cb.canonical(dict(format=cb.PAIR_FORMAT, x=x, z=z, atlasSha256=ATLAS, geometrySha256=geometry_sha,
                               blocksSha256=blocks_sha))
    pair_sha = hashlib.sha256(record).hexdigest()
    cb._immutable(store / 'pairs' / f'{pair_sha}.json', record)
    return pair_sha, blocks_sha, geometry_sha


class StoreCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(os.path.realpath(tmp.name)) / 'out'
        self.store = self.out / 'store'
        for name in ('pairs', 'objects', 'indexes'):
            (self.store / name).mkdir(parents=True)
        self.calls = []
        original = cb._verify_pair

        def counting(store, key, pair_sha):
            self.calls.append(key)
            return original(store, key, pair_sha)
        patcher = mock.patch.object(cb, '_verify_pair', side_effect=counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def pointer(self):
        path = self.store / 'current.json'
        return json.loads(path.read_bytes())['indexSha256'] if path.exists() else None

    def pairs_of(self, sha):
        return json.loads((self.store / 'indexes' / f'{sha}.json').read_bytes())['pairs']

    def publish(self, cache, expected, updates=None, removals=frozenset(), budget=None):
        return cws.publish_incremental(self.store, expected_index=expected, updates=updates or {},
                                       removals=set(removals), cache=cache,
                                       budget=budget or cws.Budget(10_000, 1 << 34))


class RealCodecTests(StoreCase):
    def test_first_publication_then_only_new_pairs_are_read(self):
        cache = cws.PairProofCache()
        pairs = {f'{tx},0': make_pair(self.store, tx, 0)[0] for tx in range(3)}
        first = self.publish(cache, None, pairs)
        self.assertEqual(self.pointer(), first['indexSha256'])
        self.assertEqual(sorted(self.calls), sorted(pairs))
        self.assertGreater(first['bytesRead'], 0)
        raw = (self.store / 'indexes' / f'{first["indexSha256"]}.json').read_bytes()
        self.assertEqual(raw, cb.canonical({'format': cb.INDEX_FORMAT, 'tileSize': 64, 'pairs': pairs}),
                         'same canonical index bytes as collision_bundle.publish_index')
        self.assertEqual((self.store / 'current.json').read_bytes(),
                         cb.canonical({'format': cb.POINTER_FORMAT, 'indexSha256': first['indexSha256']}))
        self.calls.clear()
        new = make_pair(self.store, 0, 1)[0]
        second = self.publish(cache, first['indexSha256'], {'0,1': new})
        self.assertEqual(self.calls, ['0,1'], 'warm proofs: only the new pair is read')
        self.assertEqual(second['entries'], 4)
        self.assertEqual(self.pairs_of(second['indexSha256']), {**pairs, '0,1': new})

    def test_restarted_process_never_trusts_the_persisted_index(self):
        pairs = {f'{tx},0': make_pair(self.store, tx, 0)[0] for tx in range(3)}
        first = self.publish(cws.PairProofCache(), None, pairs)
        self.calls.clear()
        again = self.publish(cws.PairProofCache(), first['indexSha256'])       # fresh cache = new process
        self.assertTrue(again['unchanged'])
        self.assertEqual(sorted(self.calls), sorted(pairs), 'every previous pair verified again')

    def test_changed_time_inode_or_content_forces_reverification(self):
        cache = cws.PairProofCache()
        pair_sha, blocks_sha, geometry_sha = make_pair(self.store, 0, 0)
        current = self.publish(cache, None, {'0,0': pair_sha})['indexSha256']
        blocks = self.store / 'objects' / f'{blocks_sha}.uib'
        geometry = self.store / 'objects' / f'{geometry_sha}.uih'

        def republish():
            self.calls.clear()
            self.publish(cache, current)
            return list(self.calls)

        self.assertEqual(republish(), [], 'unchanged files: cache hit')
        os.utime(blocks, ns=(1, 1))                                    # mtime (and ctime) change
        self.assertEqual(republish(), ['0,0'])
        self.assertEqual(republish(), [])
        time.sleep(0.05)
        os.chmod(geometry, 0o640)                                       # ctime only
        self.assertEqual(republish(), ['0,0'])
        replacement = blocks.with_name('replacement')
        replacement.write_bytes(blocks.read_bytes())
        os.replace(replacement, blocks)                                 # same bytes, new inode
        self.assertEqual(republish(), ['0,0'])
        raw = ct.encode_raw(FakeVolume(0, 0, height=10), 0, 0)          # different content under the old name
        blocks.write_bytes(ct.compressed(raw))
        with self.assertRaisesRegex(cws.StoreError, 'failed verification'):
            self.publish(cache, current)
        self.assertEqual(self.pointer(), current)

    def test_corrupt_or_linked_objects_are_refused(self):
        cache = cws.PairProofCache()
        good = make_pair(self.store, 0, 0)[0]
        current = self.publish(cache, None, {'0,0': good})['indexSha256']

        bad_sha, blocks_sha, _ = make_pair(self.store, 1, 0)
        path = self.store / 'objects' / f'{blocks_sha}.uib'
        path.write_bytes(path.read_bytes()[:100])                       # truncated gzip
        with self.assertRaises(cws.StoreError):
            self.publish(cache, current, {'1,0': bad_sha})

        linked_sha, blocks_sha, _ = make_pair(self.store, 2, 0)
        path = self.store / 'objects' / f'{blocks_sha}.uib'
        target = self.out / 'outside.uib'
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(cws.StoreError, 'non-symlink'):
            self.publish(cache, current, {'2,0': linked_sha})

        with self.assertRaises(cws.StoreError):                         # pair for another tile
            self.publish(cache, current, {'5,5': good})
        self.assertEqual(self.pointer(), current)
        self.assertEqual(self.pairs_of(current), {'0,0': good})

    def test_removal_and_contradictory_input(self):
        cache = cws.PairProofCache()
        pairs = {f'{tx},0': make_pair(self.store, tx, 0)[0] for tx in range(3)}
        current = self.publish(cache, None, pairs)['indexSha256']
        self.calls.clear()
        result = self.publish(cache, current, removals={'1,0', '9,9'})
        self.assertEqual(result['removed'], 1)
        self.assertEqual(self.pairs_of(result['indexSha256']), {k: v for k, v in pairs.items() if k != '1,0'})
        self.assertEqual(self.calls, [])
        with self.assertRaisesRegex(cws.StoreError, 'updated and removed'):
            self.publish(cache, result['indexSha256'], {'0,0': pairs['0,0']}, {'0,0'})


class WarmupAndCasTests(StoreCase):
    def test_cold_cache_warms_up_across_bounded_calls(self):
        pairs = {f'{tx},0': make_pair(self.store, tx, 0)[0] for tx in range(5)}
        cache = cws.PairProofCache()
        pending = 0
        for attempt in range(10):
            try:
                result = self.publish(cache, None, pairs, budget=cws.Budget(max_reads=4, max_bytes=1 << 30))
                break
            except cws.WarmupPending:
                pending += 1
                self.assertIsNone(self.pointer(), 'nothing published while warming up')
                self.assertEqual(len(list((self.store / 'indexes').iterdir())), 0)
        self.assertEqual(pending, 4, 'one pair (4 reads: record twice + 2 objects) per call')
        self.assertEqual(sorted(self.calls), sorted(pairs), 'no pair verified twice')
        self.assertEqual(self.pointer(), result['indexSha256'])

    def test_budget_counts_real_sizes_and_always_progresses(self):
        made = {f'{tx},0': make_pair(self.store, tx, 0) for tx in range(2)}
        pairs = {key: value[0] for key, value in made.items()}
        cache = cws.PairProofCache()
        budget = cws.Budget(max_reads=100, max_bytes=1)                 # smaller than any pair
        with self.assertRaises(cws.WarmupPending):
            self.publish(cache, None, pairs, budget=budget)
        self.assertEqual(len(cache), 1, 'the first pair proceeds despite the byte limit')
        pair_sha, blocks_sha, geometry_sha = made['0,0']
        sizes = [(self.store / 'pairs' / f'{pair_sha}.json').stat().st_size,
                 (self.store / 'objects' / f'{blocks_sha}.uib').stat().st_size,
                 (self.store / 'objects' / f'{geometry_sha}.uih').stat().st_size]
        self.assertEqual(budget.bytes, 2 * sizes[0] + sizes[1] + sizes[2],
                         'real sizes: the pair record is read twice (own read + collision_bundle), objects once')
        self.assertEqual(budget.reads, 4)
        self.assertEqual(self.calls, ['0,0'])

    def test_partial_attempt_counts_the_record_read_and_oversized_objects_are_refused_unread(self):
        made = {f'{tx},0': make_pair(self.store, tx, 0) for tx in range(2)}
        pairs = {key: value[0] for key, value in made.items()}
        cache = cws.PairProofCache()
        budget = cws.Budget(max_reads=5, max_bytes=1 << 30)
        with self.assertRaises(cws.WarmupPending):
            self.publish(cache, None, pairs, budget=budget)
        second_record = (self.store / 'pairs' / f'{made["1,0"][0]}.json').stat().st_size
        first = made['0,0']
        first_bytes = (2 * (self.store / 'pairs' / f'{first[0]}.json').stat().st_size
                       + (self.store / 'objects' / f'{first[1]}.uib').stat().st_size
                       + (self.store / 'objects' / f'{first[2]}.uih').stat().st_size)
        self.assertEqual(budget.reads, 5, 'second record was read, then its objects did not fit')
        self.assertEqual(budget.bytes, first_bytes + second_record)
        self.assertEqual(self.calls, ['0,0'])

        self.calls.clear()
        with mock.patch.object(cb, 'MAX_OBJECT_BYTES', 64):
            with self.assertRaisesRegex(cws.StoreError, 'larger than'):
                self.publish(cws.PairProofCache(), None, pairs)
        self.assertEqual(self.calls, [], 'refused before collision_bundle reads the object')

    def test_full_small_cache_can_replace_a_tile_and_too_small_cache_is_refused(self):
        cache = cws.PairProofCache(max_entries=1)
        old = make_pair(self.store, 0, 0)[0]
        current = self.publish(cache, None, {'0,0': old})['indexSha256']
        new = make_pair(self.store, 0, 0, height=10)[0]                 # same tile, new blocks
        self.assertNotEqual(new, old)
        result = self.publish(cache, current, {'0,0': new})
        self.assertEqual(self.pairs_of(result['indexSha256']), {'0,0': new})
        self.assertEqual(len(cache), 1)
        other = make_pair(self.store, 1, 0)[0]
        with self.assertRaisesRegex(cws.StoreError, 'capacity'):
            self.publish(cache, result['indexSha256'], {'1,0': other})
        self.assertEqual(self.pointer(), result['indexSha256'])

    def test_unchanged_result_requires_every_proof_to_still_hold(self):
        pairs = {f'{tx},0': make_pair(self.store, tx, 0) for tx in range(2)}
        current = self.publish(cws.PairProofCache(), None, {k: v[0] for k, v in pairs.items()})['indexSha256']
        original = cb._verify_pair

        def touching(store, key, pair_sha):
            result = original(store, key, pair_sha)
            if key == '1,0':
                os.utime(self.store / 'objects' / f'{pairs["0,0"][1]}.uib', ns=(7, 7))  # first pair changes
            return result
        cache = cws.PairProofCache()                                     # cold, same index
        with mock.patch.object(cb, '_verify_pair', side_effect=touching):
            with self.assertRaisesRegex(cws.WarmupPending, 'changed after verification'):
                self.publish(cache, current)
        self.calls.clear()
        again = self.publish(cache, current)
        self.assertTrue(again['unchanged'])
        self.assertEqual(self.calls, ['0,0'], 'only the changed pair is verified again')

    def test_pointer_changed_before_or_during_verification_refuses(self):
        cache = cws.PairProofCache()
        first = make_pair(self.store, 0, 0)[0]
        current = self.publish(cache, None, {'0,0': first})['indexSha256']
        with self.assertRaisesRegex(cws.StoreError, 'concurrent'):
            self.publish(cache, 'f' * 64, {})
        with self.assertRaisesRegex(cws.StoreError, 'concurrent'):
            self.publish(cache, None, {'0,0': first})

        new = make_pair(self.store, 1, 0)[0]
        other = cb.canonical({'format': cb.POINTER_FORMAT, 'indexSha256': 'e' * 64})
        original = cb._verify_pair

        def moving(store, key, pair_sha):
            (self.store / 'current.json').write_bytes(other)            # another publisher wins meanwhile
            return original(store, key, pair_sha)
        before = set((self.store / 'indexes').iterdir())
        with mock.patch.object(cb, '_verify_pair', side_effect=moving):
            with self.assertRaisesRegex(cws.StoreError, 'concurrent'):
                self.publish(cache, current, {'1,0': new})
        self.assertEqual((self.store / 'current.json').read_bytes(), other, 'never overwritten')
        self.assertEqual(set((self.store / 'indexes').iterdir()), before, 'no index written')

    def test_file_changed_after_its_proof_blocks_publication_until_reverified(self):
        cache = cws.PairProofCache()
        first, blocks_a, _ = make_pair(self.store, 0, 0)
        second = make_pair(self.store, 1, 0)[0]
        original = cb._verify_pair

        def touching(store, key, pair_sha):
            result = original(store, key, pair_sha)
            if key == '1,0':
                os.utime(self.store / 'objects' / f'{blocks_a}.uib', ns=(5, 5))   # A changes after its proof
            return result
        with mock.patch.object(cb, '_verify_pair', side_effect=touching):
            with self.assertRaisesRegex(cws.WarmupPending, 'changed after verification'):
                self.publish(cache, None, {'0,0': first, '1,0': second})
        self.assertIsNone(self.pointer())
        result = self.publish(cache, None, {'0,0': first, '1,0': second})
        self.assertEqual(self.pointer(), result['indexSha256'])

    def test_previous_index_must_be_valid(self):
        cache = cws.PairProofCache()
        current = self.publish(cache, None, {'0,0': make_pair(self.store, 0, 0)[0]})['indexSha256']
        path = self.store / 'indexes' / f'{current}.json'
        raw = path.read_bytes()
        os.chmod(path, 0o644)
        path.write_bytes(raw.replace(b'"tileSize":64', b'"tileSize":65'))
        with self.assertRaisesRegex(cws.StoreError, 'does not match its hash'):
            self.publish(cache, current)
        path.write_bytes(raw)
        (self.store / 'current.json').write_bytes(b'{"format":"UIE_COLLISION_POINTER1","indexSha256":"' + current.encode()
                                                  + b'","format":"UIE_COLLISION_POINTER1"}')
        with self.assertRaisesRegex(cws.StoreError, 'duplicate'):
            self.publish(cache, current)


class GcHookTests(StoreCase):
    def test_journal_is_recorded_before_the_pointer_and_its_failure_publishes_nothing(self):
        import collision_gc
        (self.out / cb.GC_ENABLED_MARKER).write_text('')
        cache = cws.PairProofCache()
        first = make_pair(self.store, 0, 0)[0]
        seen = []

        def record(out, old, new, now):
            seen.append((Path(out), old, new, self.pointer()))
        with mock.patch.object(collision_gc, 'record_publication', side_effect=record):
            result = self.publish(cache, None, {'0,0': first})
        self.assertEqual(seen, [(self.out, None, result['indexSha256'], None)], 'journal before pointer replacement')
        self.assertEqual(self.pointer(), result['indexSha256'])

        second = make_pair(self.store, 1, 0)[0]
        with mock.patch.object(collision_gc, 'record_publication', side_effect=OSError('journal disk full')):
            with self.assertRaisesRegex(cws.StoreError, 'journal'):
                self.publish(cache, result['indexSha256'], {'1,0': second})
        self.assertEqual(self.pointer(), result['indexSha256'], 'old pointer kept')

        other = cb.canonical({'format': cb.POINTER_FORMAT, 'indexSha256': 'e' * 64})

        def racing(out, old, new, now):
            (self.store / 'current.json').write_bytes(other)            # someone publishes during the journal
        with mock.patch.object(collision_gc, 'record_publication', side_effect=racing):
            with self.assertRaisesRegex(cws.StoreError, 'concurrent'):
                self.publish(cache, result['indexSha256'], {'1,0': second})
        self.assertEqual((self.store / 'current.json').read_bytes(), other, 'never overwritten after the journal')

    def test_index_directory_is_synced_before_the_pointer(self):
        cache = cws.PairProofCache()
        first = make_pair(self.store, 0, 0)[0]
        events = []
        original_sync, original_replace = cb._fsync_directory, cb._replace_atomic

        def sync(path):
            events.append(('fsync', Path(path).name))
            return original_sync(path)

        def replace(path, payload):
            events.append(('pointer', Path(path).name))
            return original_replace(path, payload)
        with mock.patch.object(cb, '_fsync_directory', side_effect=sync), \
                mock.patch.object(cb, '_replace_atomic', side_effect=replace):
            self.publish(cache, None, {'0,0': first})
        self.assertLess(events.index(('fsync', 'indexes')), events.index(('pointer', 'current.json')))
        self.assertEqual(events[-1], ('fsync', 'store'))


class LimitTests(unittest.TestCase):
    """SYNTHETIC: payload verification is stubbed; only index counting, size and schema limits are exercised."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(os.path.realpath(tmp.name)) / 'store'
        for name in ('pairs', 'objects', 'indexes'):
            (self.store / name).mkdir(parents=True)
        self.cache = mock.Mock(spec=cws.PairProofCache)
        self.cache.max_entries = cb.MAX_INDEX_ENTRIES
        self.cache.verify.return_value = 0
        self.cache.proven.return_value = True

    def keys(self, n, offset=0):
        return {f'{i % 1000 - 500},{i // 1000 + offset}': hashlib.sha256(str(i).encode()).hexdigest() for i in range(n)}

    def write_index(self, pairs):
        raw = cb.canonical({'format': cb.INDEX_FORMAT, 'tileSize': 64, 'pairs': pairs})
        sha = hashlib.sha256(raw).hexdigest()
        (self.store / 'indexes' / f'{sha}.json').write_bytes(raw)
        (self.store / 'current.json').write_bytes(cb.canonical({'format': cb.POINTER_FORMAT, 'indexSha256': sha}))
        return sha

    def test_whole_world_index_fits_and_count_limit_refuses_before_verification(self):
        world = self.keys(56_782)
        current = self.write_index(world)
        result = cws.publish_incremental(self.store, expected_index=current, updates=self.keys(1, offset=900),
                                         removals=set(), cache=self.cache, budget=cws.Budget(1, 1))
        self.assertEqual(result['entries'], 56_783)
        self.assertLessEqual((self.store / 'indexes' / f'{result["indexSha256"]}.json').stat().st_size,
                             cb.MAX_INDEX_BYTES)
        self.cache.verify.reset_mock()
        with self.assertRaisesRegex(cws.StoreError, 'entries'):
            cws.publish_incremental(self.store, expected_index=result['indexSha256'],
                                    updates=self.keys(90_001 - 56_783, offset=200), removals=set(),
                                    cache=self.cache, budget=cws.Budget(1, 1))
        self.cache.verify.assert_not_called()

    def test_size_limit_refuses_before_verification(self):
        with mock.patch.object(cb, 'MAX_INDEX_BYTES', 2000):
            with self.assertRaisesRegex(cws.StoreError, '8 MiB'):
                cws.publish_incremental(self.store, expected_index=None, updates=self.keys(50), removals=set(),
                                        cache=self.cache, budget=cws.Budget(1, 1))
        self.cache.verify.assert_not_called()
        self.assertFalse((self.store / 'current.json').exists())

    def test_invalid_inputs_are_refused(self):
        for kwargs in ({'updates': {'01,0': 'a' * 64}}, {'updates': {'0,0': 'xyz'}}, {'removals': {'a,b'}},
                       {'updates': [('0,0', 'a' * 64)]}):
            with self.subTest(kwargs):
                args = dict(expected_index=None, updates={}, removals=set(), cache=self.cache,
                            budget=cws.Budget(1, 1))
                args.update(kwargs)
                with self.assertRaises(cws.StoreError):
                    cws.publish_incremental(self.store, **args)
        with self.assertRaises(ValueError):
            cws.Budget(0, 1)
        with self.assertRaises(TypeError):
            cws.publish_incremental(self.store, updates={}, removals=set(), cache=self.cache,
                                    budget=cws.Budget(1, 1))   # expected_index is mandatory


if __name__ == '__main__':
    unittest.main()

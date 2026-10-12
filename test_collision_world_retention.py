"""Tests of the read-only collision_world_retention planner and pure selection (POSIX: ctime identities).

Real objects: UIB1 via collision_tile.encode_raw (small stand-in Volume) + gzip, UIH1 via world_hires.encode_tile,
canonical pairs and indexes, real collision_world_store.PairProofCache / collision_bundle._verify_pair (wrapped only
to count calls). The tests delete files themselves only to simulate a future executor between plans.
    python3 -m unittest test_collision_world_retention -v
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
import collision_world_retention as cwr
import collision_world_store as cws
import world_hires as wh

ATLAS = 'a' * 64
OLD = 100_000


class FakeVolume:
    registry = {0: 'minecraft:air', 1: 'minecraft:stone'}

    def __init__(self, x0, z0, height=64):
        self.x0, self.z0 = x0, z0
        self.present = {(x0 // 16 + cx, z0 // 16 + cz) for cx in range(4) for cz in range(4)}
        self.ids = array('H', [1] * height + [0] * (256 - height)) * 4096
        self.metas = bytearray(4096 * 256)

    def column_index(self, x, z):
        dx, dz = x - self.x0, z - self.z0
        return dz * 64 + dx if 0 <= dx < 64 and 0 <= dz < 64 else -1


def make_pair(store, tx, tz, height=64):
    """(pairSha, blocks object name, geometry object name), written like collision_bundle.publish_pair."""
    x, z = tx * 64, tz * 64
    raw = ct.encode_raw(FakeVolume(x, z, height), x, z)
    blocks = ct.identity(raw)
    cb._immutable(store / 'objects' / f'{blocks}.uib', ct.compressed(raw))
    data = wh.encode_tile(x, z, [{'positions': [x, 64, z, x + 1, 64, z, x + 1, 64, z + 1, x, 64, z + 1],
                                   'uvs': [0, 0, 1, 0, 1, 1, 0, 1], 'colors': [1.0] * 12}])
    geometry = hashlib.sha256(data).hexdigest()
    cb._immutable(store / 'objects' / f'{geometry}.uih', data)
    record = cb.canonical(dict(format=cb.PAIR_FORMAT, x=x, z=z, atlasSha256=ATLAS, geometrySha256=geometry,
                               blocksSha256=blocks))
    sha = hashlib.sha256(record).hexdigest()
    cb._immutable(store / 'pairs' / f'{sha}.json', record)
    return sha, f'{blocks}.uib', f'{geometry}.uih'


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(os.path.realpath(tmp.name)) / 'store'
        for name in ('indexes', 'pairs', 'objects'):
            (self.store / name).mkdir(parents=True)
        self.now = time.time()
        self.calls = []
        original = cb._verify_pair

        def counting(store, key, pair_sha):
            self.calls.append((key, pair_sha))
            return original(store, key, pair_sha)
        patcher = mock.patch.object(cb, '_verify_pair', side_effect=counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def index(self, pairs, *, age=None):
        raw = cb.canonical({'format': cb.INDEX_FORMAT, 'tileSize': 64, 'pairs': pairs})
        sha = hashlib.sha256(raw).hexdigest()
        path = self.store / 'indexes' / f'{sha}.json'
        if not path.exists():
            path.write_bytes(raw)
        if age is not None:
            self.age(path, age)
        return sha

    def point(self, sha):
        (self.store / 'current.json').write_bytes(cb.canonical({'format': cb.POINTER_FORMAT, 'indexSha256': sha}))

    def age(self, path, seconds=OLD):
        ns = int((self.now - seconds) * 1e9)
        os.utime(path, ns=(ns, ns))

    def age_all(self):
        for folder in ('indexes', 'pairs', 'objects'):
            for path in (self.store / folder).iterdir():
                self.age(path)

    def plan(self, cache=None, *, journal=None, budget=None, limits=None, now=None):
        # Explicit None checks: an EMPTY PairProofCache is falsy (len 0) and must still be the cache that is used.
        return cwr.plan_world_retention(self.store, now=self.now if now is None else now,
                                        journal={'updatedAt': self.now, 'indexes': {}} if journal is None else journal,
                                        cache=cws.PairProofCache() if cache is None else cache,
                                        budget=cws.Budget(10_000, 1 << 34) if budget is None else budget,
                                        limits=limits)

    def paths(self, plan, phase):
        return [c['path'] for c in plan['candidates'][phase]]


class PlanTests(Case):
    def test_old_indexes_are_never_parsed_even_malformed_and_drain_oldest_first(self):
        a = make_pair(self.store, 0, 0)[0]
        b = make_pair(self.store, 1, 0)[0]
        old = self.index({'0,0': a})
        garbage = 'd' * 64
        (self.store / 'indexes' / f'{garbage}.json').write_bytes(b'not json at all')
        current = self.index({'0,0': a, '1,0': b})
        self.point(current)
        self.age_all()
        self.age(self.store / 'indexes' / f'{old}.json', 90_000)
        self.age(self.store / 'indexes' / f'{garbage}.json', 80_000)
        read = []
        original = cws._read

        def reading(path, limit):
            read.append(Path(path).parent.name + '/' + Path(path).name)
            return original(path, limit)
        with mock.patch.object(cws, '_read', side_effect=reading):
            plan = self.plan()
        self.assertEqual([p for p in read if p.startswith('indexes/')], [f'indexes/{current}.json'])
        self.assertEqual(self.paths(plan, 'indexes'), [f'indexes/{old}.json', f'indexes/{garbage}.json'])
        self.assertFalse(plan['pairPhaseAllowed'])
        self.assertFalse(plan['objectPhaseAllowed'])
        json.dumps(plan)                                              # plain JSON
        self.assertEqual(plan['roots'], {current: 'current'})

    def test_two_historical_revisions_of_one_key_are_both_retained(self):
        first = make_pair(self.store, 0, 0)
        second = make_pair(self.store, 0, 0, height=10)
        protected = self.index({'0,0': first[0]})
        current = self.index({'0,0': second[0]})
        self.point(current)
        self.age_all()
        cache = cws.PairProofCache()
        plan = self.plan(cache, journal={'updatedAt': self.now, 'indexes': {protected: self.now - 10}})
        self.assertEqual(plan['roots'], {current: 'current', protected: 'journal'})
        self.assertEqual(plan['retained']['proofs'], 2)
        self.assertEqual(len(cache), 2, 'both revisions proven, nothing evicted')
        self.assertEqual(plan['candidates'], {'indexes': [], 'pairs': [], 'objects': []})
        self.assertTrue(plan['objectPhaseAllowed'])

        with self.assertRaisesRegex(cwr.RetentionRefused, 'capacity'):
            self.plan(cws.PairProofCache(max_entries=1),
                      journal={'updatedAt': self.now, 'indexes': {protected: self.now - 10}})
        other = make_pair(self.store, 2, 0)[0]
        self.age_all()
        full = cws.PairProofCache(max_entries=2)
        full.verify(self.store, '2,0', other, cws.Budget(10, 1 << 30))    # a foreign proof occupies a slot
        with self.assertRaisesRegex(cwr.RetentionRefused, 'capacity'):
            self.plan(full, journal={'updatedAt': self.now, 'indexes': {protected: self.now - 10}})
        self.assertIn((str(self.store), '2,0', other), full._proofs, 'the planner never evicts proofs')

    def test_cold_cache_warms_up_across_bounded_calls(self):
        pairs = {f'{tx},0': make_pair(self.store, tx, 0)[0] for tx in range(3)}
        self.point(self.index(pairs))
        self.age_all()
        cache = cws.PairProofCache()
        pending = 0
        for _ in range(5):
            try:
                plan = self.plan(cache, budget=cws.Budget(4, 1 << 30))
                break
            except cwr.WarmupPending:
                pending += 1
        self.assertEqual(pending, 2, 'one pair (4 reads) per call')
        self.assertEqual(sorted(k for k, _ in self.calls), ['0,0', '1,0', '2,0'], 'no pair verified twice')
        self.assertEqual(plan['retained']['proofs'], 3)

    def test_young_pair_keeps_its_old_objects_and_orphans_are_object_candidates(self):
        a = make_pair(self.store, 0, 0)[0]
        young, blocks, geometry = make_pair(self.store, 1, 0)
        orphan_pair, orphan_blocks, _ = make_pair(self.store, 2, 0, height=20)
        self.point(self.index({'0,0': a}))
        self.age_all()
        (self.store / 'pairs' / f'{orphan_pair}.json').unlink()
        os.utime(self.store / 'pairs' / f'{young}.json')               # only the pair record is young
        plan = self.plan()
        self.assertNotIn(f'pairs/{young}.json', self.paths(plan, 'pairs'))
        objects = self.paths(plan, 'objects')
        self.assertNotIn(f'objects/{blocks}', objects, 'young pair closure protected')
        self.assertNotIn(f'objects/{geometry}', objects)
        self.assertIn(f'objects/{orphan_blocks}', objects)
        self.assertTrue(plan['objectPhaseAllowed'])

    def test_pointer_change_during_planning_refuses(self):
        a = make_pair(self.store, 0, 0)[0]
        self.point(self.index({'0,0': a}))
        self.age_all()
        other = self.index({}, age=OLD)
        original = cb._verify_pair

        def moving(store, key, pair_sha):
            self.point(other)
            return original(store, key, pair_sha)
        with mock.patch.object(cb, '_verify_pair', side_effect=moving):
            with self.assertRaisesRegex(cwr.RetentionRefused, 'changed during planning'):
                self.plan()

    def test_pair_changed_while_a_later_pair_was_verified_blocks_the_plan_until_reverified(self):
        a, a_blocks, _ = make_pair(self.store, 0, 0)
        b = make_pair(self.store, 1, 0)[0]
        self.point(self.index({'0,0': a, '1,0': b}))
        self.age_all()
        cache = cws.PairProofCache()
        original = cb._verify_pair

        def touching(store, key, pair_sha):
            result = original(store, key, pair_sha)
            if key == '1,0':
                os.utime(self.store / 'objects' / a_blocks, ns=(5, 5))    # A changes after its proof
            return result
        with mock.patch.object(cb, '_verify_pair', side_effect=touching):
            with self.assertRaisesRegex(cwr.WarmupPending, 'changed after verification'):
                self.plan(cache)
        self.calls.clear()
        plan = self.plan(cache)
        self.assertEqual([k for k, _ in self.calls], ['0,0'], 'only the changed pair is verified again')
        self.assertEqual(plan['retained']['proofs'], 2)

    def test_root_index_replaced_after_it_was_read_refuses(self):
        a = make_pair(self.store, 0, 0)[0]
        current = self.index({'0,0': a})
        self.point(current)
        self.age_all()
        path = self.store / 'indexes' / f'{current}.json'
        original = cb._verify_pair

        def replacing(store, key, pair_sha):
            copy = path.with_name('replacement')
            copy.write_bytes(path.read_bytes())                        # same bytes and hash, new inode
            os.replace(copy, path)
            return original(store, key, pair_sha)
        with mock.patch.object(cb, '_verify_pair', side_effect=replacing):
            with self.assertRaisesRegex(cwr.RetentionRefused, f'indexes/{current}.json changed during planning'):
                self.plan()

    def test_scan_counts_every_entry_without_entering_foreign_directories(self):
        a = make_pair(self.store, 0, 0)[0]
        self.point(self.index({'0,0': a}))
        foreign = self.store / 'foreign'
        foreign.mkdir()
        for i in range(50):
            (foreign / f'f{i}').write_text('x')
        self.age_all()
        # top level: current.json, indexes, pairs, objects, foreign = 5; inside: 1 index + 1 pair + 2 objects = 4
        plan = self.plan(limits=cwr.WorldRetentionLimits(max_files=9))
        self.assertEqual(plan['unknown']['examples'], ['foreign'])
        with self.assertRaisesRegex(cwr.RetentionRefused, 'more than 8 files'):
            self.plan(limits=cwr.WorldRetentionLimits(max_files=8))

    def test_metadata_and_file_quotas_fail_closed(self):
        pairs = {f'{tx},0': make_pair(self.store, tx, 0)[0] for tx in range(2)}
        self.point(self.index(pairs))
        self.age_all()
        for limits, message in ((cwr.WorldRetentionLimits(max_root_index_bytes=10), 'root indexes'),
                                (cwr.WorldRetentionLimits(max_pair_bytes=10), 'pair records'),
                                (cwr.WorldRetentionLimits(max_files=3), 'files')):
            with self.subTest(message):
                with self.assertRaisesRegex(cwr.RetentionRefused, message):
                    self.plan(limits=limits)

    def test_corrupt_or_misbound_retained_data_refuses(self):
        a, blocks, _ = make_pair(self.store, 0, 0)
        current = self.index({'0,0': a})
        self.point(current)
        self.age_all()
        wrong = self.index({'5,5': a}, age=OLD)                         # same pair under the wrong key
        self.point(wrong)
        with self.assertRaisesRegex(cwr.RetentionRefused, 'retained pair 5,5'):
            self.plan()
        self.point(current)
        path = self.store / 'objects' / blocks
        data = path.read_bytes()
        path.write_bytes(data[:50])
        with self.assertRaisesRegex(cwr.RetentionRefused, 'failed verification'):
            self.plan()
        path.write_bytes(data)
        index = self.store / 'indexes' / f'{current}.json'
        index.write_bytes(index.read_bytes().replace(b'"tileSize":64', b'"tileSize":65'))
        with self.assertRaisesRegex(cwr.RetentionRefused, 'does not match its hash'):
            self.plan()

    def test_unknown_names_are_counted_never_candidates_and_symlinks_refuse(self):
        a = make_pair(self.store, 0, 0)[0]
        self.point(self.index({'0,0': a}))
        (self.store / 'pairs' / 'notes.txt').write_text('x')
        (self.store / 'objects' / '.object-tmp').write_text('x')
        (self.store / 'foreign').mkdir()
        self.age_all()
        plan = self.plan()
        self.assertEqual(plan['unknown']['count'], 3)
        everything = [c['path'] for phase in cwr.PHASES for c in plan['candidates'][phase]]
        self.assertFalse([p for p in everything if 'notes' in p or 'object-tmp' in p or 'foreign' in p])
        (self.store / 'objects' / ('e' * 64 + '.uib')).symlink_to(self.store / 'pairs' / 'notes.txt')
        with self.assertRaisesRegex(cwr.RetentionRefused, 'symlink'):
            self.plan()

    def test_inputs_are_validated(self):
        a = make_pair(self.store, 0, 0)[0]
        self.point(self.index({'0,0': a}))
        with self.assertRaises(ValueError):
            self.plan(now=float('nan'))
        with self.assertRaises(cwr.RetentionRefused):
            self.plan(journal={'updatedAt': self.now, 'indexes': {'f' * 64: float('inf')}})
        with self.assertRaises(cwr.RetentionRefused):
            self.plan(journal={'updatedAt': self.now})
        with self.assertRaises(ValueError):
            cwr.WorldRetentionLimits(max_files=0)
        with self.assertRaises(ValueError):
            cwr.plan_world_retention(self.store, now=self.now, journal={'updatedAt': self.now, 'indexes': {}},
                                     cache=cws.PairProofCache(), budget=cws.Budget(1, 1), grace_seconds=60)


class PhaseAndSelectionTests(Case):
    def test_indexes_then_pairs_then_objects_each_after_its_own_grace(self):
        old_pair, old_blocks, _ = make_pair(self.store, 0, 0)
        new_pair = make_pair(self.store, 0, 0, height=10)[0]
        old_index = self.index({'0,0': old_pair})
        self.point(self.index({'0,0': new_pair}))
        self.age_all()
        cache = cws.PairProofCache()

        plan = self.plan(cache)
        self.assertEqual(self.paths(plan, 'indexes'), [f'indexes/{old_index}.json'])
        self.assertEqual(self.paths(plan, 'pairs'), [f'pairs/{old_pair}.json'], 'reported, not allowed')
        self.assertFalse(plan['pairPhaseAllowed'])
        first = cwr.select_deletions(plan, {}, now=self.now)
        self.assertEqual(list(first['entries']), [f'indexes/{old_index}.json'], 'pairs not observed yet')
        self.assertEqual(first['selected'], [])
        self.assertEqual(first['suppressedPhases'], ['pairs', 'objects'])
        later = cwr.select_deletions(plan, first['entries'], now=self.now + 1800)
        self.assertEqual([c['path'] for c in later['selected']], [f'indexes/{old_index}.json'])
        self.assertEqual(later['phase'], 'indexes')

        (self.store / 'indexes' / f'{old_index}.json').unlink()        # what a future executor would do
        plan = self.plan(cache)
        self.assertTrue(plan['pairPhaseAllowed'])
        self.assertFalse(plan['objectPhaseAllowed'], 'an unretained pair still exists')
        self.assertIn(f'objects/{old_blocks}', self.paths(plan, 'objects'))
        pairs = cwr.select_deletions(plan, {}, now=self.now)
        self.assertEqual(list(pairs['entries']), [f'pairs/{old_pair}.json'], 'objects not observed yet')
        pairs = cwr.select_deletions(plan, pairs['entries'], now=self.now + 1800)
        self.assertEqual([c['path'] for c in pairs['selected']], [f'pairs/{old_pair}.json'])

        (self.store / 'pairs' / f'{old_pair}.json').unlink()
        plan = self.plan(cache)
        self.assertTrue(plan['objectPhaseAllowed'])
        objects = cwr.select_deletions(plan, {}, now=self.now)
        objects = cwr.select_deletions(plan, objects['entries'], now=self.now + 1800)
        self.assertEqual([c['path'] for c in objects['selected']], [f'objects/{old_blocks}'])
        self.assertEqual(objects['phase'], 'objects')


class SelectionTests(Case):
    def setUp(self):
        super().setUp()
        a = make_pair(self.store, 0, 0)[0]
        self.old = [self.index({'0,0': a}), self.index({}), ]
        self.point(self.index({'0,0': a, '1,0': make_pair(self.store, 1, 0)[0]}))
        self.age_all()
        self.age(self.store / 'indexes' / f'{self.old[0]}.json', 90_000)
        self.age(self.store / 'indexes' / f'{self.old[1]}.json', 80_000)
        self.planned = self.plan()
        self.items = {c['path']: c for c in self.planned['candidates']['indexes']}

    def entry(self, rel, first, **changes):
        item = dict(self.items[rel])
        item.pop('path')
        item.update(changes)
        return {'firstSeen': first, **item}

    def test_grace_requires_same_identity_including_ctime_and_never_ages_the_future(self):
        one, two = list(self.items)
        ledger = {one: self.entry(one, self.now - 4000), two: self.entry(two, self.now - 4000, ctimeNs=1)}
        decision = cwr.select_deletions(self.planned, ledger, now=self.now)
        self.assertEqual([c['path'] for c in decision['selected']], [one])
        self.assertEqual(decision['entries'][two]['firstSeen'], self.now, 'changed ctime resets the grace')
        future = cwr.select_deletions(self.planned, {one: self.entry(one, self.now + 10_000)}, now=self.now)
        self.assertEqual(future['selected'], [])
        self.assertEqual(future['entries'][one]['firstSeen'], self.now)
        json.dumps(decision)

    def test_budgets_and_ledger_validation(self):
        one, two = list(self.items)
        ledger = {one: self.entry(one, self.now - 4000), two: self.entry(two, self.now - 4000)}
        decision = cwr.select_deletions(self.planned, ledger, now=self.now,
                                        limits=cwr.WorldRetentionLimits(max_delete_files=1))
        self.assertEqual(len(decision['selected']), 1)
        self.assertEqual(decision['skipped']['fileBudget'], 1)
        for bad in ({'indexes/x.json': self.entry(one, 1.0)},
                    {one: self.entry(one, float('inf'))},
                    {one: self.entry(one, 1.0, size=True)},
                    {one: {**self.entry(one, 1.0), 'extra': 1}}):
            with self.assertRaises(cwr.RetentionRefused):
                cwr.select_deletions(self.planned, bad, now=self.now)
        with self.assertRaisesRegex(cwr.RetentionRefused, 'bounded'):
            cwr.select_deletions(self.planned, ledger, now=self.now,
                                 limits=cwr.WorldRetentionLimits(max_ledger_entries=1))
        with self.assertRaisesRegex(cwr.RetentionRefused, 'byte limit'):
            cwr.select_deletions(self.planned, {}, now=self.now, limits=cwr.WorldRetentionLimits(max_ledger_bytes=20))
        broken = dict(self.planned, objectPhaseAllowed=True, pairPhaseAllowed=False)
        with self.assertRaises(cwr.RetentionRefused):
            cwr.select_deletions(broken, {}, now=self.now)

    def forged(self, **changes):
        plan = json.loads(json.dumps(self.planned))
        plan.update(changes)
        return plan

    def test_forged_or_stale_plans_are_refused(self):
        one = list(self.items)[0]
        ledger = {one: self.entry(one, self.now - 4000)}
        pair_item = {'path': 'pairs/' + 'c' * 64 + '.json', 'size': 10, 'mtimeNs': 1, 'ctimeNs': 1, 'ino': 1, 'dev': 1}
        contradictory = self.forged(pairPhaseAllowed=True)            # index candidates still listed
        contradictory['candidates']['pairs'] = [pair_item]
        with self.assertRaisesRegex(cwr.RetentionRefused, 'inconsistent'):
            cwr.select_deletions(contradictory, ledger, now=self.now + 4000,
                                 limits=cwr.WorldRetentionLimits(max_delete_bytes=1))
        negative = self.forged()
        negative['candidates']['indexes'][0]['size'] = -1
        with self.assertRaisesRegex(cwr.RetentionRefused, 'invalid plan candidate'):
            cwr.select_deletions(negative, ledger, now=self.now)
        duplicate = self.forged()
        duplicate['candidates']['indexes'].append(dict(duplicate['candidates']['indexes'][0]))
        with self.assertRaisesRegex(cwr.RetentionRefused, 'duplicate'):
            cwr.select_deletions(duplicate, ledger, now=self.now)
        with self.assertRaisesRegex(cwr.RetentionRefused, 'recompute'):
            cwr.select_deletions(self.planned, ledger, now=self.now - 1)
        with self.assertRaisesRegex(cwr.RetentionRefused, 'not a world retention plan'):
            cwr.select_deletions(self.forged(createdAt=None), ledger, now=self.now)
        negative_time = self.forged()                                # timestamps may be negative (old clocks)
        negative_time['candidates']['indexes'][0]['mtimeNs'] = -5
        cwr.select_deletions(negative_time, {}, now=self.now)


if __name__ == '__main__':
    unittest.main()

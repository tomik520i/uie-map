"""Tests for collision_selection.py (pure bounded selection planner). Stdlib only.
    python -m unittest test_collision_selection -v      (from work/uie-map)
"""
import calendar
import json
import math
import time
import unittest

import collision_selection as cs

NOW = calendar.timegm(time.strptime('2026-10-04T12:00:00Z', '%Y-%m-%dT%H:%M:%SZ'))
PINNED = ['1,-2', '2,-2', '1,-1', '2,-1']


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def player(n, x, z, seen=-60, dimension=0, name=None):
    return {'uuid': f'{n:08x}-0000-4000-8000-000000000000', 'name': name or f'Player{n}', 'dimension': dimension,
            'x': x, 'y': 70.0, 'z': z, 'yaw': 0.0, 'lastSeen': iso(NOW + seen), 'online': None}


def doc(players, generated=-30):
    return {'format': 'UIE_PLAYERS1', 'generated': iso(NOW + generated), 'onlineSource': None, 'players': players}


def plan(document, **kw):
    kw.setdefault('now', NOW)
    return cs.plan_selection(document, **kw)


def around(tx, tz):
    """Expected 3 x 3 order: center first, then row by row."""
    return [f'{tx},{tz}'] + [f'{tx + dx},{tz + dz}' for dz in (-1, 0, 1) for dx in (-1, 0, 1) if (dx, dz) != (0, 0)]


class SelectionTests(unittest.TestCase):
    def test_pinned_only_for_missing_stale_future_or_malformed_documents(self):
        cases = {'missing': None, 'malformed': {'format': 'OTHER'},
                 'stale': doc([player(1, 0, 0)], generated=-601), 'future': doc([player(1, 0, 0)], generated=61)}
        cases['not a list'] = {**doc([]), 'players': {}}
        cases['bad time'] = {**doc([]), 'generated': '2026-10-04 12:00:00'}
        expected_state = {'missing': 'missing', 'malformed': 'malformed', 'stale': 'stale', 'future': 'future',
                          'not a list': 'malformed', 'bad time': 'malformed'}
        for label, document in cases.items():
            with self.subTest(label):
                result = plan(document)
                self.assertEqual(result['activeKeys'], PINNED)
                self.assertEqual(result['selectedKeys'], PINNED)
                self.assertEqual(result['document']['state'], expected_state[label])
                self.assertIsNotNone(result['document']['reason'])
                self.assertEqual(set(result['reasons'].values()), {'pinned'})
        self.assertEqual(plan(doc([player(1, -500, -500)], generated=-600))['document']['state'], 'fresh', 'exactly 10 min')
        self.assertEqual(plan(doc([player(1, -500, -500)], generated=60))['document']['state'], 'fresh', '60 s skew')

    def test_one_fresh_player_adds_its_3x3_area_with_correct_negative_floor(self):
        result = plan(doc([player(1, -0.5, -64.0)]))
        self.assertEqual(result['activeKeys'], PINNED + around(-1, -1))
        self.assertEqual(result['counts']['active'], 13)
        self.assertEqual({result['reasons'][k] for k in around(-1, -1)}, {'player-area-1'})
        self.assertEqual(result['selectedKeys'], (PINNED + around(-1, -1))[:8])
        for x, tile in ((-0.0001, -1), (0, 0), (63.999, 0), (64, 1), (-64, -1), (-64.0001, -2), (-30_000_000, -468750)):
            with self.subTest(x=x):
                self.assertEqual(plan(doc([player(1, x, 500)]))['activeKeys'][4], f'{tile},7')

    def test_overlap_with_pinned_tiles_is_deduplicated(self):
        result = plan(doc([player(1, 100, -100)]))                   # tile 1,-2 is pinned
        self.assertEqual(result['activeKeys'][:4], PINNED)
        self.assertEqual(len(result['activeKeys']), len(set(result['activeKeys'])))
        self.assertEqual(sorted(result['activeKeys'][4:]), sorted(set(around(1, -2)) - set(PINNED)))
        self.assertEqual(result['counts']['active'], 9)

    def test_player_age_and_skew(self):
        fresh = [player(1, 1000, 1000, seen=-1800), player(2, 3000, 3000, seen=60)]
        old = [player(3, 5000, 5000, seen=-1801), player(4, 7000, 7000, seen=61)]
        result = plan(doc(fresh + old), max_players=3)
        self.assertEqual(result['counts']['usedPlayers'], 2)
        self.assertEqual(result['counts']['skipped'], {'lastSeenInFuture': 1, 'lastSeenStale': 1})

    def test_malformed_records_are_skipped_and_counted(self):
        bad = [player(1, 0, 0, dimension=1), player(2, 0, 0, dimension=-1), player(3, 0, 0, dimension=True),
               player(4, 0, 0, dimension=0.0), player(5, 0, 0, dimension='0'),
               player(6, float('nan'), 0), player(7, math.inf, 0), player(8, True, 0), player(9, '5', 0),
               player(10, 30_000_001, 0), player(11, 0, -30_000_001),
               {**player(12, 0, 0), 'uuid': 'nope'}, {k: v for k, v in player(13, 0, 0).items() if k != 'uuid'},
               {**player(14, 0, 0), 'lastSeen': 'yesterday'}, 'not a record', None]
        result = plan(doc(bad))
        self.assertEqual(result['activeKeys'], PINNED)
        self.assertEqual(result['counts']['skipped'], {'badLastSeen': 1, 'badPosition': 4, 'badUuid': 2,
                                                       'notAnObject': 2, 'notOverworld': 5, 'outsideWorld': 2})
        self.assertEqual(result['counts']['records'], len(bad))

    def test_freshest_players_first_then_uuid_up_to_max_players(self):
        players = [player(3, 5000, 5000, seen=-100), player(2, 1000, 1000, seen=-10), player(1, -3000, 3000, seen=-10)]
        one = plan(doc(players))
        self.assertEqual(one['activeKeys'][4:], around(-47, 46), 'tie on lastSeen: smaller uuid wins')
        three = plan(doc(players), max_players=3)
        self.assertEqual(three['activeKeys'], PINNED + around(-47, 46) + around(15, 15) + around(78, 78))
        self.assertEqual(three['counts']['active'], 31)
        self.assertEqual(three['reasons']['78,78'], 'player-area-3')

    def test_duplicate_records_keep_the_freshest_position(self):
        result = plan(doc([player(1, 0, 6400, seen=-500), player(1, 6400, 6400, seen=-5)]), max_players=3)
        self.assertEqual(result['counts']['duplicates'], 1)
        self.assertEqual(result['activeKeys'][4:], around(100, 100))

    def test_strict_active_and_batch_caps(self):
        result = plan(doc([player(1, 10_000, 10_000), player(2, -10_000, -10_000, seen=-61)]), max_players=2,
                      max_active=10)
        self.assertEqual(len(result['activeKeys']), 10)
        self.assertEqual(result['activeKeys'][:4], PINNED)
        self.assertEqual(result['counts']['droppedByCap'], 12)
        self.assertLessEqual(len(result['selectedKeys']), 8)
        self.assertEqual(len(plan(doc([player(1, 9000, 9000)]), max_batch=3)['selectedKeys']), 3)

    def test_world_border_tiles_are_not_invented(self):
        result = plan(doc([player(1, 30_000_000, 0)]))
        self.assertEqual(result['counts']['outsideWorldTiles'], 3)
        self.assertEqual(result['counts']['active'], 10)
        self.assertTrue(all(abs(int(k.split(',')[0])) <= 468750 for k in result['activeKeys']))

    def test_rotation_covers_everything_in_three_batches_without_starving_pinned(self):
        document = doc([player(1, 10_000, 10_000), player(2, -10_000, -10_000, seen=-61)])
        attempts, batches = {}, []
        for round_ in range(4):
            result = plan(document, now=NOW + round_, previous_attempts=attempts, max_players=2)
            self.assertEqual(result['counts']['active'], 22)
            batches.append(result['selectedKeys'])
            for key in result['selectedKeys']:                  # the runner records only after a real export
                attempts[key] = NOW + round_
        self.assertEqual([len(b) for b in batches], [8, 8, 8, 8])
        self.assertEqual(batches[0][:4], PINNED, 'never attempted: active order, pinned first')
        self.assertEqual(set(batches[0] + batches[1] + batches[2]), set(result['activeKeys']), 'full coverage')
        self.assertEqual(set(batches[1] + batches[2] + batches[3]), set(result['activeKeys']),
                         'any three consecutive batches cover every key: no starvation, pinned included')
        self.assertEqual(batches[2][-2:], ['1,-2', '2,-2'], 'after the never-attempted keys: oldest attempts first')

    def test_invalid_or_future_attempts_count_as_never_attempted(self):
        attempts = {'1,-2': NOW + 120, '2,-2': 'x', '1,-1': True, '2,-1': math.nan, '99,99': NOW - 5,
                    '-1,-1': NOW - 1}
        result = plan(doc([player(1, -0.5, -64.0)]), previous_attempts=attempts)
        self.assertEqual((result['counts']['attemptsUsed'], result['counts']['attemptsIgnored']), (1, 5))
        expected = [k for k in PINNED + around(-1, -1) if k != '-1,-1'][:8]
        self.assertEqual(result['selectedKeys'], expected)
        self.assertEqual(result['counts']['neverAttempted'], 12)

    def test_invalid_options_are_refused(self):
        document = doc([])
        for kw in ({'max_active': 3}, {'max_active': 33}, {'max_active': True}, {'max_active': 10.0},
                   {'max_batch': 0}, {'max_batch': 9}, {'max_players': 0}, {'max_players': 4},
                   {'now': math.nan}, {'now': True}, {'now': '1'}, {'previous_attempts': []}):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    plan(document, **kw)

    def test_output_contains_no_player_identity(self):
        players = [player(n, 1000 * n, -1000 * n, name=f'SecretName{n}') for n in range(1, 4)]
        text = json.dumps(plan(doc(players), max_players=3))
        for p in players:
            self.assertNotIn(p['uuid'], text)
            self.assertNotIn(p['name'], text)
        self.assertIn('not a rendered result', text)


if __name__ == '__main__':
    unittest.main()

import copy
import hashlib
import unittest
from hires_source import validate_source, from_producer


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tiles = {'0,0': 'a' * 64, '1,0': 'b' * 64}
        self.source = dict(legacyIndexSha256='a'*64, producerFinishedAt=1700000000,
                           planComplete=True, plannedJobs=2, notScheduled=0,
                           failedJobs=0, failedTiles=0, staleTiles=[])

    def test_complete_and_partial(self):
        self.assertEqual(validate_source(self.source, self.tiles), self.source)
        source = dict(self.source, planComplete=False, failedTiles=1, staleTiles=['0,0'])
        result = validate_source(source, self.tiles)
        result['staleTiles'].append('1,0')
        self.assertEqual(source['staleTiles'], ['0,0'])

    def test_bad_values(self):
        for key, value in [('producerFinishedAt', 0), ('producerFinishedAt', float('nan')),
                           ('producerFinishedAt', True), ('planComplete', 1),
                           ('plannedJobs', True), ('failedTiles', -1),
                           ('failedJobs', 3), ('notScheduled', 3),
                           ('failedTiles', 2**53), ('legacyIndexSha256', 'bad'),
                           ('staleTiles', ['9,9']), ('staleTiles', ['0,0', '0,0']),
                           ('staleTiles', ['1,0', '0,0']), ('extra', 0)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_source(dict(self.source, **{key: value}), self.tiles)

    def test_complete_cannot_claim_stale_or_failed(self):
        for key, value in [('staleTiles', ['0,0']), ('failedTiles', 1),
                           ('failedJobs', 1), ('notScheduled', 1)]:
            with self.assertRaises(ValueError):
                validate_source(dict(self.source, **{key: value}), self.tiles)

    def test_receipt_time_and_stale_are_bound_to_producer(self):
        raw = b'fixture index bytes'
        legacy = {'tiles': self.tiles, 'stale': ['1,0', '0,0']}
        status = dict(format='UIE_HIRES_STATUS1', state='done', finishedAt=1234,
                      planComplete=False, plannedJobs=2, notScheduled=1,
                      failedJobs=0, failedTiles=0,
                      immutablePreparation={'legacyIndexSha256': hashlib.sha256(raw).hexdigest()})
        a = from_producer(raw, legacy, status)
        self.assertEqual(a['staleTiles'], ['0,0', '1,0'])
        self.assertEqual(a['producerFinishedAt'], 1234)
        self.assertEqual(a, from_producer(raw, legacy, status))
        with self.assertRaises(ValueError): from_producer(raw+b' ', legacy, status)
        bad = copy.deepcopy(status); bad.pop('finishedAt')
        with self.assertRaises(ValueError): from_producer(raw, legacy, bad)

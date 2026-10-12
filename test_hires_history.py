import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hires_bundle as hb
import hires_history as hh


class HistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name).resolve()
        (self.out / hb.OWNED_MARKER).write_text('test')
        (self.out / 'indexes').mkdir()

    def index(self, marker, *, v2=False):
        doc = {'format': hb.INDEX_FORMAT_V2 if v2 else hb.INDEX_FORMAT,
               'tileSize': 64, 'rendererIdentity': hashlib.sha256(f'renderer-{marker}'.encode()).hexdigest(),
               'atlasSha256': hashlib.sha256(b'atlas').hexdigest(), 'tiles': {}}
        if v2:
            doc['context'] = {'world': 'world', 'dimension': 'overworld', 'renderer': 'test',
                              'caves': False, 'layoutSha256': hashlib.sha256(b'layout').hexdigest()}
        raw = json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()
        sha = hashlib.sha256(raw).hexdigest()
        (self.out / 'indexes' / f'{sha}.json').write_bytes(raw)
        return sha

    def journal(self):
        return json.loads((self.out / hh.NAME).read_bytes())

    def test_aba_refreshes_most_recent_publication(self):
        a, b = self.index('a'), self.index('b', v2=True)
        hh.record_publication(self.out, a, now=100)
        hh.record_publication(self.out, b, a, now=200)
        report = hh.record_publication(self.out, a, b, now=300)
        doc = self.journal()
        self.assertEqual(doc['currentIndexSha256'], a)
        self.assertEqual(doc['previousIndexSha256'], b)
        self.assertEqual(doc['lastPublished'], {a: 300, b: 300})
        self.assertEqual(report['records'], 2)

    def test_long_lived_current_gets_full_grace_after_retirement(self):
        a, b, c = self.index('a'), self.index('b'), self.index('c')
        hh.record_publication(self.out, a, now=0)
        hh.record_publication(self.out, b, a, now=10_000)
        self.assertEqual(self.journal()['lastPublished'][a], 10_000)
        hh.record_publication(self.out, c, b, now=10_001)
        self.assertIn(a, self.journal()['lastPublished'])
        hh.record_publication(self.out, c, b, now=11_800)
        self.assertIn(a, self.journal()['lastPublished'])
        hh.record_publication(self.out, c, b, now=11_801)
        self.assertNotIn(a, self.journal()['lastPublished'])

    def test_missing_or_corrupt_candidate_and_previous_preserve_journal(self):
        a, b = self.index('a'), self.index('b')
        hh.record_publication(self.out, a, now=100)
        before = (self.out / hh.NAME).read_bytes()
        with self.assertRaises(FileNotFoundError):
            hh.record_publication(self.out, 'f' * 64, a, now=200)
        self.assertEqual((self.out / hh.NAME).read_bytes(), before)
        (self.out / 'indexes' / f'{b}.json').write_bytes(b'{}')
        with self.assertRaisesRegex(hb.BundleError, 'hash mismatch'):
            hh.record_publication(self.out, b, a, now=200)
        self.assertEqual((self.out / hh.NAME).read_bytes(), before)
        with self.assertRaisesRegex(hb.BundleError, 'hash mismatch'):
            hh.record_publication(self.out, a, b, now=200)
        self.assertEqual((self.out / hh.NAME).read_bytes(), before)

    def test_malformed_duplicate_and_symlink_journal_refuse(self):
        a = self.index('a')
        path = self.out / hh.NAME
        path.write_bytes(b'{}')
        with self.assertRaises(hb.BundleError):
            hh.record_publication(self.out, a, now=100)
        path.write_bytes(b'{"format":1,"format":2}')
        with self.assertRaisesRegex(hb.BundleError, 'duplicate'):
            hh.record_publication(self.out, a, now=100)
        path.unlink()
        target = self.out / 'elsewhere.json'
        target.write_bytes(b'{}')
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaisesRegex(hb.BundleError, 'regular file'):
            hh.record_publication(self.out, a, now=100)
        self.assertEqual(target.read_bytes(), b'{}')

    def test_all_recent_records_refuse_capacity_without_mutation(self):
        a, b = self.index('a'), self.index('b')
        records = {hashlib.sha256(str(i).encode()).hexdigest(): 100 for i in range(hh.MAX_RECORDS - 1)}
        records[a] = 100
        doc = {'format': hh.FORMAT, 'currentIndexSha256': a,
               'previousIndexSha256': None, 'lastPublished': records}
        path = self.out / hh.NAME
        path.write_bytes(json.dumps(doc).encode())
        before = path.read_bytes()
        with self.assertRaisesRegex(hb.BundleError, 'capacity exhausted'):
            hh.record_publication(self.out, b, a, now=101)
        self.assertEqual(path.read_bytes(), before)

    def test_old_pruning_keeps_candidate_previous_and_future_records(self):
        a, b, c = self.index('a'), self.index('b'), self.index('c')
        hh.record_publication(self.out, a, now=0)
        hh.record_publication(self.out, b, a, now=1)
        report = hh.record_publication(self.out, c, b, now=hh.GRACE_SECONDS + 2)
        self.assertEqual(set(self.journal()['lastPublished']), {b, c})
        self.assertEqual(report['pruned'], 1)
        future = hashlib.sha256(b'future').hexdigest()
        doc = self.journal()
        doc['lastPublished'][future] = 10_000
        (self.out / hh.NAME).write_bytes(json.dumps(doc).encode())
        hh.record_publication(self.out, c, b, now=hh.GRACE_SECONDS + 3)
        self.assertEqual(self.journal()['lastPublished'][future], 10_000)

    def test_io_error_propagates_and_keeps_old_journal(self):
        a, b = self.index('a'), self.index('b')
        hh.record_publication(self.out, a, now=100)
        before = (self.out / hh.NAME).read_bytes()
        with patch.object(hb, '_atomic', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                hh.record_publication(self.out, b, a, now=200)
        self.assertEqual((self.out / hh.NAME).read_bytes(), before)

    def test_invalid_time_and_duplicate_index_key_refuse(self):
        a = self.index('a')
        for now in (-1, math.inf, math.nan, True):
            with self.assertRaises(ValueError):
                hh.record_publication(self.out, a, now=now)
        raw = b'{"format":"UIE_HIRES_IMMUTABLE_INDEX1","format":"UIE_HIRES_IMMUTABLE_INDEX1"}'
        bad = hashlib.sha256(raw).hexdigest()
        (self.out / 'indexes' / f'{bad}.json').write_bytes(raw)
        with self.assertRaisesRegex(hb.BundleError, 'duplicate'):
            hh.record_publication(self.out, bad, now=100)
        self.assertFalse((self.out / hh.NAME).exists())


if __name__ == '__main__':
    unittest.main()

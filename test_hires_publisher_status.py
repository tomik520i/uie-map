import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hires_bundle as hb
import hires_publisher_status as ps


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # hb._owned requires the normalized real path (e.g. no Windows 8.3 short-name ancestor in TEMP).
        self.out = Path(self.tmp.name).resolve()
        (self.out / hb.OWNED_MARKER).write_text('test')

    def status(self):
        return (self.out / ps.NAME).read_bytes()

    def test_preparing_snapshot_replaces_complete_previous_record(self):
        ps.write_status(self.out, {'state': 'published', 'published': True,
                        'indexSha256': 'a'*64, 'entries': 100}, now=1)
        current = ps.write_status(self.out, {'state': 'preparing', 'captured': 2,
                                   'remaining': 98, 'published': False}, now=2)
        self.assertEqual(json.loads(self.status()), current)
        self.assertNotIn('indexSha256', current)
        self.assertNotIn('entries', current)

    def test_published_record_keeps_only_whitelisted_fields(self):
        doc = ps.write_status(self.out, {'state': 'published', 'published': True, 'unchanged': True,
                                         'indexSha256': 'b' * 64, 'entries': 56782,
                                         'verificationCachedTiles': 56782, 'tick': 9, 'elapsedSeconds': 1.5,
                                         'peakRssKiB': 140000, 'statusWriteError': 'x'}, now=1234.5)
        self.assertEqual(doc, {'format': ps.FORMAT, 'updatedAt': 1234.5, 'state': 'published', 'published': True,
                               'unchanged': True, 'indexSha256': 'b' * 64, 'entries': 56782,
                               'verificationCachedTiles': 56782})
        self.assertEqual(self.status(), json.dumps(doc, sort_keys=True, separators=(',', ':')).encode())
        self.assertLessEqual(len(self.status()), ps.MAX_BYTES)

    def test_public_status_does_not_expose_error_paths_or_internal_log(self):
        doc = ps.write_status(self.out, {'state': 'error', 'error': '/private/file secret',
                              'tick': 700, 'peakRssKiB': 100, 'published': False}, now=1)
        self.assertEqual(set(doc), {'format', 'updatedAt', 'state', 'published'})
        self.assertNotIn(b'secret', self.status())
        self.assertNotIn(b'private', self.status())

    def test_unknown_telemetry_is_omitted_not_zero(self):
        doc = ps.write_status(self.out, {'state': 'producer-busy', 'published': False,
                                         'verificationCachedTiles': None}, now=1)
        self.assertNotIn('verificationCachedTiles', doc)

    def test_invalid_record_preserves_previous_status(self):
        ps.write_status(self.out, {'state': 'producer-busy'}, now=1)
        before = self.status()
        for record in [{'state': 'bad'}, {'state': 'preparing', 'captured': True},
                       {'state': 'verifying', 'verificationCachedTiles': 100001},
                       {'state': 'preparing', 'remaining': -1}, {'state': 'preparing', 'captured': 1.0},
                       {'state': 'published', 'indexSha256': '../bad'},
                       {'state': 'published', 'indexSha256': 'A' * 64},
                       {'state': 'published', 'published': 1}, {'state': 'published', 'unchanged': 'yes'},
                       {'state': ['published']}, {}, 'published', None]:
            with self.subTest(record=record), self.assertRaises(ValueError):
                ps.write_status(self.out, record, now=2)
            self.assertEqual(self.status(), before)
        for now in [True, 0, -1, float('nan'), float('inf'), '1']:
            with self.subTest(now=now), self.assertRaises(ValueError):
                ps.write_status(self.out, {'state': 'verifying'}, now=now)
            self.assertEqual(self.status(), before)

    def test_size_cap_is_enforced_without_assert_and_preserves_previous(self):
        ps.write_status(self.out, {'state': 'verifying'}, now=1)
        before = self.status()
        with patch.object(ps, 'MAX_BYTES', 10), self.assertRaisesRegex(ValueError, '4 KiB'):
            ps.write_status(self.out, {'state': 'verifying'}, now=2)
        self.assertEqual(self.status(), before)

    def test_verification_cache_is_named_as_cache_not_published_coverage(self):
        doc = ps.write_status(self.out, {'state': 'verifying', 'verificationCachedTiles': 128,
                                        'published': False}, now=1)
        self.assertEqual(doc['verificationCachedTiles'], 128)
        self.assertFalse(doc['published'])
        self.assertNotIn('entries', doc)
        self.assertIn('NOT the number of tiles', ps.__doc__)

    def test_unowned_store_is_refused_without_writing(self):
        (self.out / hb.OWNED_MARKER).unlink()
        with self.assertRaises((OSError, hb.BundleError)):
            ps.write_status(self.out, {'state': 'verifying'}, now=1)
        self.assertFalse((self.out / ps.NAME).exists())

    def test_atomic_replacement_leaves_no_temporary_files(self):
        ps.write_status(self.out, {'state': 'verifying'}, now=1)
        ps.write_status(self.out, {'state': 'preparing', 'captured': 1}, now=2)
        names = sorted(os.listdir(self.out))
        self.assertEqual(names, sorted([hb.OWNED_MARKER, ps.NAME]))

    def test_symlinked_status_name_is_replaced_not_followed(self):
        target = self.out / 'elsewhere.json'
        target.write_text('unchanged')
        try:
            os.symlink(target, self.out / ps.NAME)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        ps.write_status(self.out, {'state': 'verifying'}, now=1)
        self.assertEqual(target.read_text(), 'unchanged')
        self.assertFalse((self.out / ps.NAME).is_symlink())


if __name__ == '__main__':
    unittest.main()

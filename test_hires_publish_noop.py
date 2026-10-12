"""Republishing the verified current index is a no-op for current.json and a valid journal. Stdlib only.
    python -m unittest test_hires_publish_noop -v      (from work/uie-map)
"""
import json
import os
import unittest

import hires_bundle as hb
import hires_history as hh
import test_hires_publish_monotonic as fixtures   # module only: its TestCase is not re-collected here


class NoopTests(unittest.TestCase):
    setUp = fixtures.MonotonicTests.setUp
    context = fixtures.MonotonicTests.context
    source = fixtures.MonotonicTests.source
    publish = fixtures.MonotonicTests.publish
    write_index = fixtures.MonotonicTests.write_index

    def pointer_state(self):
        path = self.out / 'current.json'
        info = os.stat(path)
        return path.read_bytes(), info.st_ino, info.st_mtime_ns

    def journal_raw(self):
        path = self.out / hh.NAME
        return path.read_bytes() if path.exists() else None

    def journal(self):
        return json.loads(self.journal_raw())

    def test_identical_republication_keeps_pointer_bytes_and_inode(self):
        first = self.publish(2)
        self.assertFalse(first['unchanged'])
        before = self.pointer_state()
        again = self.publish(2)
        self.assertEqual((again['indexSha256'], again['unchanged']), (first['indexSha256'], True))
        self.assertEqual(self.pointer_state(), before)
        self.assertIsNone(self.journal_raw(), 'record_history=False never creates a journal')

    def test_a_b_repeat_b_keeps_journal_bytes_and_previous_a(self):
        a = self.publish(2, record_history=True)
        b = self.publish(3, record_history=True)
        self.assertFalse(b['unchanged'])
        journal, pointer = self.journal_raw(), self.pointer_state()
        for _ in range(3):
            self.assertTrue(self.publish(3, record_history=True)['unchanged'])
        self.assertEqual(self.journal_raw(), journal, 'journal bytes stable')
        self.assertEqual(self.pointer_state(), pointer)
        doc = self.journal()
        self.assertEqual((doc['currentIndexSha256'], doc['previousIndexSha256']), (b['indexSha256'], a['indexSha256']))

    def test_absent_journal_is_repaired_without_touching_the_pointer(self):
        first = self.publish(2)
        pointer = self.pointer_state()
        self.assertTrue(self.publish(2, record_history=True)['unchanged'])
        self.assertEqual(self.pointer_state(), pointer)
        doc = self.journal()
        self.assertEqual((doc['currentIndexSha256'], doc['previousIndexSha256']), (first['indexSha256'], None))
        stable = self.journal_raw()
        self.publish(2, record_history=True)
        self.assertEqual(self.journal_raw(), stable, 'repaired once, then stable')

    def test_candidate_journal_from_a_crash_before_the_pointer_is_repaired(self):
        a = self.publish(2, record_history=True)['indexSha256']
        doc = json.loads((self.out / 'indexes' / f'{a}.json').read_bytes())
        candidate = self.write_index(json.dumps({**doc, 'rendererIdentity': 'c' * 64},
                                                sort_keys=True, separators=(',', ':')).encode())
        hh.record_publication(self.out, candidate, a)          # journal names C, the pointer still names A
        pointer = self.pointer_state()
        self.assertTrue(self.publish(2, record_history=True)['unchanged'])
        self.assertEqual(self.pointer_state(), pointer)
        journal = self.journal()
        self.assertEqual(journal['currentIndexSha256'], a)
        self.assertEqual(journal['previousIndexSha256'], candidate, 'the other journal generation stays a root')
        self.assertIn(candidate, journal['lastPublished'])

    def test_corrupted_journal_fails_closed(self):
        self.publish(2, record_history=True)
        pointer = self.pointer_state()
        for raw in (b'not json', b'{"format":"x"}', json.dumps({'format': hh.FORMAT}).encode()):
            with self.subTest(raw=raw):
                (self.out / hh.NAME).write_bytes(raw)
                with self.assertRaises(hb.BundleError):
                    self.publish(2, record_history=True)
                self.assertEqual(self.journal_raw(), raw, 'corrupt journal left for inspection')
                self.assertEqual(self.pointer_state(), pointer)

    def test_corrupted_tile_is_refused_even_for_the_current_sha(self):
        self.publish(2, record_history=True)
        pointer, journal = self.pointer_state(), self.journal_raw()
        target = self.out / 'objects' / f"{self.refs['1,0']}.uih"
        tmp = self.out / 'objects' / 'replacement.tmp'
        tmp.write_bytes(fixtures.wh.encode_tile(64, 0, [fixtures.quad(66, 64, 1)]))
        os.replace(tmp, target)                                # same name, different content
        with self.assertRaisesRegex(hb.BundleError, 'hash mismatch'):
            self.publish(2, record_history=True)
        self.assertEqual((self.pointer_state(), self.journal_raw()), (pointer, journal))

    def test_a_different_index_still_replaces_the_pointer(self):
        a = self.publish(2, record_history=True)
        b = self.publish(2, context=self.context(renderer='other'), record_history=True)
        self.assertFalse(b['unchanged'])
        self.assertNotEqual(a['indexSha256'], b['indexSha256'])
        self.assertEqual(json.loads((self.out / 'current.json').read_bytes())['indexSha256'], b['indexSha256'])
        self.assertEqual(self.journal()['previousIndexSha256'], a['indexSha256'])


if __name__ == '__main__':
    unittest.main()

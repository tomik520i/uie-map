import unittest
from unittest.mock import patch
import cave_pair_history as history
import cave_pair_publish as publisher
import test_cave_pair_publish as fixture
import hires_bundle as hb

class HistoryIntegrationTests(unittest.TestCase):
    setUp=fixture.PublishTests.setUp
    write_geometry=fixture.PublishTests.write_geometry
    write_job=fixture.PublishTests.write_job
    publish=fixture.PublishTests.publish
    cap_pointer=fixture.PublishTests.cap_pointer

    def test_initial_history_durable_before_pointer_and_retry_recovers(self):
        atomic=hb._atomic;root=self.out/'cave-data';writes=[]
        def fail_pointer(path,raw):
            writes.append(path.name)
            if path==root/'current.json':
                self.assertIsNotNone(history.load(root))
                raise OSError('injected crash before pointer')
            return atomic(path,raw)
        with patch.object(hb,'_atomic',side_effect=fail_pointer):
            with self.assertRaisesRegex(OSError,'injected'):self.publish()
        self.assertEqual(writes,['cap-history.json','current.json'])
        self.assertFalse((root/'current.json').exists())
        self.assertTrue(self.publish()['published'])
        self.assertEqual(history.load(root)['currentIndexSha256'],self.publish()['indexSha256'])

    def test_history_write_failure_prevents_pointer_commit(self):
        with patch.object(hb,'_atomic',side_effect=OSError('history write failed')):
            with self.assertRaises(OSError):self.publish()
        self.assertFalse((self.out/'cave-data/current.json').exists())

    def test_existing_publication_bootstraps_history_then_no_rewrite(self):
        self.publish();root=self.out/'cave-data';pointer=self.cap_pointer()
        (root/history.NAME).unlink()
        result=self.publish();self.assertTrue(result['fastPath'])
        before=(root/history.NAME).read_bytes()
        with patch.object(hb,'_atomic',side_effect=AssertionError('unexpected write')):
            self.assertTrue(self.publish()['fastPath'])
        self.assertEqual(before,(root/history.NAME).read_bytes())
        self.assertEqual(pointer,self.cap_pointer())

    def test_corrupt_history_fails_closed_even_on_fast_path(self):
        self.publish();pointer=self.cap_pointer()
        (self.out/'cave-data'/history.NAME).write_bytes(b'{}')
        with self.assertRaises(ValueError):self.publish()
        self.assertEqual(pointer,self.cap_pointer())

    def test_corrupt_root_history_prevents_first_pointer(self):
        (self.out/'publication-history.json').write_bytes(b'{}')
        with self.assertRaises(ValueError):self.publish()
        self.assertFalse((self.out/'cave-data/current.json').exists())

if __name__=='__main__':unittest.main()

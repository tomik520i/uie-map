import math
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import hires_bundle as hb
import hires_publisher as hp


@unittest.skipUnless(os.name == 'posix', 'requires POSIX flock')
class PublisherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name).resolve()       # hb._owned requires the normalized real path
        (self.out / hb.OWNED_MARKER).write_text('private test')

    def once(self):
        return hp.run_once(self.out, object(), world='uie', dimension='0')

    def test_busy_lock_does_not_invoke_tick(self):
        import fcntl
        with (self.out / '.build.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(hp, 'tick') as tick:
                self.assertEqual(self.once()['state'], 'producer-busy')
                tick.assert_not_called()

    def test_release_after_exception(self):
        with patch.object(hp, 'tick', side_effect=hb.BundleError('bad data')):
            with self.assertRaises(hb.BundleError):
                self.once()
        with patch.object(hp, 'tick', return_value={'state': 'done'}) as tick:
            self.assertEqual(self.once()['state'], 'done')
            tick.assert_called_once()

    def test_symlink_lock_refused(self):
        target = self.out / 'other'
        target.write_text('unchanged')
        (self.out / '.build.lock').symlink_to(target)
        with patch.object(hp, 'tick') as tick:
            with self.assertRaises(OSError):
                self.once()
            tick.assert_not_called()
        self.assertEqual(target.read_text(), 'unchanged')

    def test_same_session_error_recovery_and_unlocked_wait(self):
        import fcntl
        records, sessions = [], []
        class Stop:
            def is_set(inner): return False
            def wait(inner, interval):
                with (self.out / '.build.lock').open('a') as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def tick(out, session, **kwargs):
            sessions.append(session)
            if len(sessions) == 1: raise hb.BundleError('broken')
            return {'state': 'published', 'published': True}
        with patch.object(hp, 'tick', side_effect=tick):
            count = hp.run(self.out, world='uie', dimension='0', max_ticks=2,
                           stop=Stop(), emit=records.append)
        self.assertEqual(count, 2)
        self.assertIs(sessions[0], sessions[1])
        self.assertEqual([r['state'] for r in records], ['error', 'published'])

    def test_stop_prevents_next_tick(self):
        stop = threading.Event()
        with patch.object(hp, 'tick', return_value={'state': 'waiting'}) as tick:
            count = hp.run(self.out, world='uie', dimension='0', max_ticks=0,
                           stop=stop, emit=lambda record: stop.set())
        self.assertEqual(count, 1)
        tick.assert_called_once()

    def test_cave_publication_is_explicit_and_default_surface_flags_unchanged(self):
        with patch.object(hp,'tick',return_value={'state':'published','published':True}) as tick:
            self.once()
            self.assertNotIn('publish_cave_pairs',tick.call_args.kwargs)
            hp.run(self.out,world='uie',dimension='0',cave_pairs=True,emit=lambda _:None)
            self.assertTrue(tick.call_args.kwargs['publish_cave_pairs'])
            self.assertTrue(tick.call_args.kwargs['allow_initial_partial_caves'])
        for value in [1,'yes',None]:
            with self.assertRaises(ValueError):hp.run(self.out,world='uie',dimension='0',cave_pairs=value)

    def test_public_status_opt_in_and_write_failure_does_not_repeat_publication(self):
        import hires_publisher_status as ps
        records=[]
        with patch.object(hp, 'tick', return_value={'state':'published','published':True}):
            hp.run(self.out, world='uie', dimension='0', emit=records.append)
        self.assertFalse((self.out/ps.NAME).exists())
        with patch.object(hp, 'tick', return_value={'state':'verifying','published':False}):
            hp.run(self.out, world='uie', dimension='0', emit=records.append, write_status=True)
        self.assertEqual(json.loads((self.out/ps.NAME).read_bytes())['state'], 'verifying')
        with patch.object(hp, 'tick', return_value={'state':'published','published':True}) as tick, \
             patch.object(ps, 'write_status', side_effect=OSError('status unavailable')):
            hp.run(self.out, world='uie', dimension='0', emit=records.append, write_status=True)
        tick.assert_called_once()
        self.assertTrue(records[-1]['published'])
        self.assertIn('statusWriteError', records[-1])
        self.assertEqual(json.loads((self.out/ps.NAME).read_bytes())['state'], 'verifying',
                         'the previous status stays until the next successful write')

    def test_unexpected_status_failure_neither_crashes_nor_repeats_the_tick(self):
        import hires_publisher_status as ps
        records = []
        stop = threading.Event()
        def emit(record):
            records.append(record)
            if len(records) == 2: stop.set()
        result = {'state': 'published', 'published': True, 'indexSha256': 'c' * 64, 'entries': 3}
        with patch.object(hp, 'tick', return_value=result) as tick, \
             patch.object(ps, 'write_status', side_effect=RuntimeError('unexpected')), \
             patch.object(stop, 'wait'):
            count = hp.run(self.out, world='uie', dimension='0', max_ticks=0, stop=stop, emit=emit,
                           write_status=True)
        self.assertEqual((count, tick.call_count), (2, 2), 'one tick per loop iteration, the loop survived')
        self.assertTrue(all(r['published'] and 'statusWriteError' in r for r in records))

    def test_status_file_of_error_and_published_ticks_is_whitelisted(self):
        import hires_publisher_status as ps
        with patch.object(hp, 'tick', side_effect=hb.BundleError(f'{self.out}/secret path')):
            hp.run(self.out, world='uie', dimension='0', write_status=True, emit=lambda record: None)
        raw = (self.out/ps.NAME).read_bytes()
        self.assertNotIn(b'secret', raw)
        self.assertEqual(set(json.loads(raw)), {'format', 'updatedAt', 'state', 'published',
                                                'verificationCachedTiles'})
        result = {'state': 'published', 'published': True, 'unchanged': False, 'indexSha256': 'd' * 64,
                  'entries': 56782}
        with patch.object(hp, 'tick', return_value=result):
            hp.run(self.out, world='uie', dimension='0', write_status=True, emit=lambda record: None)
        doc = json.loads((self.out/ps.NAME).read_bytes())
        self.assertEqual({k: doc[k] for k in result}, result)
        self.assertNotIn('tick', doc)
        self.assertNotIn('elapsedSeconds', doc)
        self.assertNotIn('peakRssKiB', doc)

    def test_records_carry_telemetry_from_the_same_session(self):
        records = []
        class Stop:
            def is_set(inner): return False
            def wait(inner, interval): time.sleep(0.3)      # never part of elapsedSeconds
        def tick(out, session, **kwargs):
            proofs = session.cache._proofs                  # the real PublishSession cache
            proofs[('objects/x.uih', f'{len(proofs)},0', 'a' * 64)] = (1, 2, 3, 4, 5, 6)
            time.sleep(0.05)
            if len(proofs) == 2: raise hb.BundleError('broken')
            return {'state': 'verifying', 'published': False}
        with patch.object(hp, 'tick', side_effect=tick):
            hp.run(self.out, world='uie', dimension='0', max_ticks=3, stop=Stop(), emit=records.append)
        self.assertEqual([r['state'] for r in records], ['verifying', 'error', 'verifying'])
        self.assertEqual([r['verificationCachedTiles'] for r in records], [1, 2, 3])
        for record in records:
            elapsed = record['elapsedSeconds']
            self.assertIsInstance(elapsed, float)
            self.assertTrue(math.isfinite(elapsed))
            self.assertGreaterEqual(elapsed, 0.04)
            self.assertLess(elapsed, 0.3, 'the wait between ticks is not counted')
            if sys.platform.startswith('linux'):
                self.assertIsInstance(record['peakRssKiB'], int)
                self.assertGreater(record['peakRssKiB'], 0)
            else:
                self.assertIsNone(record['peakRssKiB'])

    def test_busy_lock_record_also_has_telemetry(self):
        import fcntl
        records = []
        with (self.out / '.build.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(hp, 'tick') as tick:
                hp.run(self.out, world='uie', dimension='0', max_ticks=1, emit=records.append)
                tick.assert_not_called()
        self.assertEqual(records[0]['state'], 'producer-busy')
        self.assertEqual(records[0]['verificationCachedTiles'], 0)
        self.assertGreaterEqual(records[0]['elapsedSeconds'], 0)

    def test_invalid_options_and_unowned_store_rejected(self):
        for interval in [0, float('nan'), float('inf'), True, 3601]:
            with self.assertRaises(ValueError):
                hp.run(self.out, world='uie', dimension='0', interval=interval)
        (self.out / hb.OWNED_MARKER).unlink()
        with self.assertRaises((OSError, hb.BundleError)):
            self.once()


class TelemetryHelperTests(unittest.TestCase):
    """Pure helpers; no lock needed, so they also run where flock is unavailable."""

    def test_peak_rss_is_null_when_it_cannot_be_measured(self):
        with patch.object(hp.sys, 'platform', 'linux'), patch.dict(sys.modules, {'resource': None}):
            self.assertIsNone(hp._peak_rss_kib(), 'missing resource module')
        zero = types.SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda who: types.SimpleNamespace(ru_maxrss=0))
        with patch.object(hp.sys, 'platform', 'linux'), patch.dict(sys.modules, {'resource': zero}):
            self.assertIsNone(hp._peak_rss_kib(), '0 is not a measurement')
        broken = types.SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda who: (_ for _ in ()).throw(OSError('no')))
        with patch.object(hp.sys, 'platform', 'linux'), patch.dict(sys.modules, {'resource': broken}):
            self.assertIsNone(hp._peak_rss_kib())
        with patch.object(hp.sys, 'platform', 'darwin'):
            self.assertIsNone(hp._peak_rss_kib(), 'other units than KiB')
        fake = types.SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda who: types.SimpleNamespace(ru_maxrss=12345))
        with patch.object(hp.sys, 'platform', 'linux'), patch.dict(sys.modules, {'resource': fake}):
            self.assertEqual(hp._peak_rss_kib(), 12345)

    def test_cached_tile_count_helper(self):
        session = types.SimpleNamespace(cache=types.SimpleNamespace(_proofs={'a': 1, 'b': 2}))
        self.assertEqual(hp._cached_tiles(session), 2)
        self.assertIsNone(hp._cached_tiles(object()), 'unknown, not 0')

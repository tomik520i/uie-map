"""Bounded wait for the hires .build.lock (world_hires.acquire_build_lock, --lock-wait-seconds). Stdlib only.
    python -m unittest test_hires_lock_wait -v      (from work/uie-map)
Lock tests use real file descriptors and flock (POSIX only); validation tests run everywhere.
"""
import contextlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import world_hires as wh

POSIX = os.name == 'posix'


class ValidationTests(unittest.TestCase):
    def test_invalid_wait_values_are_refused(self):
        for value in (-0.1, 300.01, math.nan, math.inf, -math.inf, True, '5', None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    wh.acquire_build_lock(None, value)
        for text in ('-1', '301', 'nan', 'inf', 'abc', ''):
            with self.subTest(text=text):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    wh.main(['--world', 'w', '--catalog-dir', 'c', '--out', 'o', '--lock-wait-seconds', text])
                self.assertEqual(raised.exception.code, 2)

    def test_valid_boundaries(self):
        self.assertEqual(wh._lock_wait_arg('0'), 0.0)
        self.assertEqual(wh._lock_wait_arg('300'), 300.0)
        self.assertEqual(wh._lock_wait_arg('60'), 60.0)


@unittest.skipUnless(POSIX, 'flock lock wait requires POSIX')
class LockWaitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        self.path = self.out / '.build.lock'

    def holder(self):
        """A second, independent open file description holding the lock, like another process (or publisher)."""
        import fcntl
        handle = open(self.path, 'a')
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle

    def contender(self):
        handle = open(self.path, 'a')
        self.addCleanup(handle.close)
        return handle

    def assert_held_by(self, handle):
        import fcntl
        with open(self.path, 'a') as probe:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_free_lock_is_acquired_at_once(self):
        handle = self.contender()
        started = time.monotonic()
        self.assertTrue(wh.acquire_build_lock(handle, 5))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assert_held_by(handle)

    def test_busy_lock_with_zero_wait_skips_immediately(self):
        self.holder()
        started = time.monotonic()
        self.assertFalse(wh.acquire_build_lock(self.contender(), 0))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_lock_is_acquired_after_release(self):
        holder = self.holder()
        threading.Timer(0.5, holder.close).start()          # closing the descriptor releases its flock
        handle = self.contender()
        started = time.monotonic()
        self.assertTrue(wh.acquire_build_lock(handle, 10))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertLess(elapsed, 3)
        self.assert_held_by(handle)

    def test_wait_is_bounded_and_nothing_is_held_on_timeout(self):
        holder = self.holder()
        handle = self.contender()
        started = time.monotonic()
        self.assertFalse(wh.acquire_build_lock(handle, 0.7))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.65)
        self.assertLess(elapsed, 2.5)
        holder.close()
        import fcntl
        with open(self.path, 'a') as probe:                   # the timed-out contender did not keep or take it
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_retry_interval_and_deadline(self):
        self.holder()
        now = [100.0]
        sleeps = []
        def sleep(seconds):
            sleeps.append(round(seconds, 6))
            now[0] += seconds
        self.assertFalse(wh.acquire_build_lock(self.contender(), 0.5, clock=lambda: now[0], sleep=sleep))
        self.assertEqual(sleeps[:3], [0.2, 0.2, 0.1], 'retries every 0.2 s, last sleep only the remaining time')
        self.assertLessEqual(len(sleeps), 4)                    # at most a float-rounding remainder after that
        self.assertAlmostEqual(sum(sleeps), 0.5, places=6)

    def run_main(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wh.main(['--world', str(self.out / 'w'), '--catalog-dir', str(self.out / 'c'),
                            '--out', str(self.out), *extra])
        return code, json.loads(out.getvalue())

    def test_cli_busy_keeps_the_old_skip_and_marks_a_wait_timeout(self):
        self.holder()
        with patch.object(wh, 'build', side_effect=AssertionError('must not build without the lock')):
            self.assertEqual(self.run_main(), (0, {'state': 'skipped: another build holds the lock'}))
            code, doc = self.run_main('--lock-wait-seconds', '0.4')
        self.assertEqual(code, 0)
        self.assertEqual(doc, {'state': 'skipped: another build still holds the lock after waiting',
                               'lockWaitSeconds': 0.4})

    def test_cli_builds_once_the_lock_is_free(self):
        holder = self.holder()
        threading.Timer(0.3, holder.close).start()
        calls = []
        def build(*args, **kw):
            calls.append(kw)
            return {'state': 'done'}
        with patch.object(wh, 'build', side_effect=build):
            code, doc = self.run_main('--lock-wait-seconds', '5', '--prepare-immutable')
        self.assertEqual((code, doc), (0, {'state': 'done'}))
        self.assertEqual(calls, [{'immutable_prepare': True, 'caves': False, 'cave_caps': False}])


if __name__ == '__main__':
    unittest.main()

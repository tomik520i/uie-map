import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hires_bundle as hb
import hires_gc as gc
import hires_history as hh
import hires_retention as retention


@unittest.skipUnless(os.name == 'posix', 'GC executor requires POSIX flock')
class GCTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name).resolve()
        (self.out / hb.OWNED_MARKER).write_text('test')
        for folder in ('indexes', 'objects', 'atlases', 'layouts', 'tiles', 'jobs'):
            (self.out / folder).mkdir()
        self.now = 100_000_000_000
        self.current_tile = self.asset('objects', '.uih', b'current-tile')
        self.current_atlas = self.asset('atlases', '.png', b'current-atlas')
        self.current = self.index('current', self.current_tile, self.current_atlas)
        hh.record_publication(self.out, self.current, now=self.now - 100)
        (self.out / 'current.json').write_bytes(json.dumps({'format': hb.POINTER_FORMAT,
                                                            'indexSha256': self.current}).encode())

    def asset(self, folder, suffix, raw):
        sha = hashlib.sha256(raw).hexdigest()
        (self.out / folder / f'{sha}{suffix}').write_bytes(raw)
        return sha

    def index(self, label, tile, atlas):
        doc = {'format': hb.INDEX_FORMAT, 'tileSize': 64,
               'rendererIdentity': hashlib.sha256(label.encode()).hexdigest(),
               'atlasSha256': atlas, 'tiles': {'0,0': tile}}
        raw = json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()
        return self.asset('indexes', '.json', raw)

    def test_default_dry_run_and_explicit_execution_preserve_current_and_legacy(self):
        orphan = self.asset('objects', '.uih', b'orphan')
        (self.out / 'index.json').write_bytes(b'legacy')
        (self.out / 'atlas.png').write_bytes(b'legacy-atlas')
        (self.out / 'tiles' / '0_0.uih').write_bytes(b'legacy-tile')
        dry = gc.collect(self.out, now=self.now)
        self.assertEqual((dry['state'], dry['removed']), ('dry-run', 0))
        self.assertIn(f'objects/{orphan}.uih', dry['candidates'])
        self.assertTrue((self.out / 'objects' / f'{orphan}.uih').exists())
        done = gc.collect(self.out, execute=True, now=self.now)
        self.assertEqual((done['state'], done['removed'], done['bytes']), ('executed', 1, 6))
        self.assertFalse((self.out / 'objects' / f'{orphan}.uih').exists())
        for path in (self.out / 'current.json', self.out / hh.NAME,
                     self.out / 'indexes' / f'{self.current}.json',
                     self.out / 'objects' / f'{self.current_tile}.uih',
                     self.out / 'index.json', self.out / 'atlas.png',
                     self.out / 'tiles' / '0_0.uih'):
            self.assertTrue(path.exists(), path)

    def test_history_and_malformed_progress_guard(self):
        prior_tile = self.asset('objects', '.uih', b'prior-tile')
        prior_atlas = self.asset('atlases', '.png', b'prior-atlas')
        prior = self.index('prior', prior_tile, prior_atlas)
        hh.record_publication(self.out, self.current, prior, now=self.now - 50)
        orphan = self.asset('objects', '.uih', b'orphan')
        (self.out / 'backfill-progress.json').write_bytes(b'malformed')
        result = gc.collect(self.out, execute=True, now=self.now)
        self.assertEqual(result['removed'], 0)
        self.assertTrue((self.out / 'objects' / f'{orphan}.uih').exists())
        (self.out / 'backfill-progress.json').unlink()
        result = gc.collect(self.out, execute=True, now=self.now)
        self.assertEqual(result['removed'], 1)
        self.assertTrue((self.out / 'indexes' / f'{prior}.json').exists())
        self.assertTrue((self.out / 'objects' / f'{prior_tile}.uih').exists())

    def test_file_and_byte_limits_are_hard(self):
        for i in range(5):
            self.asset('objects', '.uih', f'orphan-{i}'.encode())
        result = gc.collect(self.out, execute=True, max_files=2, max_bytes=16, now=self.now)
        self.assertLessEqual(result['removed'], 2)
        self.assertLessEqual(result['bytes'], 16)
        self.assertEqual(result['removed'], result['selected'])
        self.assertGreater(result['candidateCount'], result['removed'])
        with self.assertRaises(ValueError):
            gc.collect(self.out, execute=True, max_files=129)
        with self.assertRaises(ValueError):
            gc.collect(self.out, execute=True, max_bytes=64 * 1024 * 1024 + 1)

    def test_partial_unlink_failure_still_syncs_and_releases_lock(self):
        import fcntl
        first = self.asset('objects', '.uih', b'orphan-one')
        second = self.asset('objects', '.uih', b'orphan-two')
        original_unlink = os.unlink
        calls = 0

        def fail_second(path, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('unlink failed')
            return original_unlink(path, *args, **kwargs)

        with patch.object(gc.os, 'unlink', side_effect=fail_second):
            with patch.object(hb, '_fsync_dir', side_effect=OSError('fsync failed')) as sync:
                with self.assertRaisesRegex(OSError, 'unlink failed'):
                    gc.collect(self.out, execute=True, now=self.now)
                sync.assert_called_once_with(self.out / 'objects')
        remaining = sum((self.out / 'objects' / f'{sha}.uih').exists() for sha in (first, second))
        self.assertEqual(remaining, 1)
        with (self.out / '.build.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_changed_identity_and_symlink_refuse_without_unlink(self):
        orphan = self.asset('objects', '.uih', b'orphan')
        target = self.out / 'objects' / f'{orphan}.uih'
        original_plan = retention.plan_retention

        def replaced(out, **kwargs):
            plan = original_plan(out, **kwargs)
            replacement = self.out / 'replacement'
            replacement.write_bytes(target.read_bytes())
            os.replace(replacement, target)
            return plan

        with patch.object(gc.retention, 'plan_retention', side_effect=replaced):
            with self.assertRaisesRegex(hb.BundleError, 'changed'):
                gc.collect(self.out, execute=True, now=self.now)
        self.assertTrue(target.exists())

        def symlinked(out, **kwargs):
            plan = original_plan(out, **kwargs)
            target.unlink()
            target.symlink_to(self.out / 'current.json')
            return plan

        with patch.object(gc.retention, 'plan_retention', side_effect=symlinked):
            with self.assertRaisesRegex(hb.BundleError, 'regular file'):
                gc.collect(self.out, execute=True, now=self.now)
        self.assertTrue((self.out / 'current.json').exists())

    def test_busy_lock_returns_without_planning(self):
        import fcntl
        with (self.out / '.build.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(gc.retention, 'plan_retention') as plan:
                result = gc.collect(self.out, execute=True, now=self.now)
            self.assertEqual(result['state'], 'producer-busy')
            plan.assert_not_called()

    def test_symlink_lock_refused(self):
        lock = self.out / '.build.lock'
        lock.symlink_to(self.out / 'current.json')
        with patch.object(gc.retention, 'plan_retention') as plan:
            with self.assertRaises(OSError):
                gc.collect(self.out, execute=True, now=self.now)
            plan.assert_not_called()

    # ------------------------------------------------------------------ telemetry (task 140)

    def test_planner_telemetry_in_dry_run_and_execute(self):
        self.asset('objects', '.uih', b'orphan')
        for execute in (False, True):
            with self.subTest(execute=execute):
                result = gc.collect(self.out, execute=execute, now=self.now)
                plan = retention.plan_retention(self.out, now=self.now)
                self.assertEqual(result['retainedIndexes'], 1)
                self.assertIs(result['preparationProgressValid'], False)
                self.assertIsInstance(result['scanned'], int)
                self.assertGreaterEqual(result['scanned'], 3)      # current index, tile, atlas (+ orphan)
                self.assertGreater(result['metadataBytes'], 0)
                self.assertLessEqual(result['metadataBytes'], retention.MAX_METADATA_BYTES)
                if execute:                                         # after unlink the new plan scans one file less
                    self.assertEqual(result['scanned'], plan['scanned'] + 1)
        prior_tile = self.asset('objects', '.uih', b'prior-tile')
        prior = self.index('prior', prior_tile, self.current_atlas)
        hh.record_publication(self.out, self.current, prior, now=self.now - 50)
        self.assertEqual(gc.collect(self.out, now=self.now)['retainedIndexes'], 2)

    # ------------------------------------------------------------------ CLI (task 140)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        # The CLI has no --now: the planner's wall clock is moved past the fixture's grace period instead.
        with patch.object(retention.time, 'time', return_value=self.now), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gc.main(['--out', str(self.out), *args])
        return code, out.getvalue(), err.getvalue()

    def test_cli_default_is_one_dry_run(self):
        orphan = self.out / 'objects' / f"{self.asset('objects', '.uih', b'orphan')}.uih"
        with patch.object(gc, 'collect', wraps=gc.collect) as collect:
            code, out, err = self.cli()
        self.assertEqual((code, err), (0, ''))
        collect.assert_called_once()
        self.assertEqual(collect.call_args.kwargs, {'execute': False, 'max_files': gc.MAX_FILES,
                                                     'max_bytes': gc.MAX_BYTES})
        doc = json.loads(out)
        self.assertEqual((doc['state'], doc['executed'], doc['removed']), ('dry-run', False, 0))
        self.assertIn(orphan.relative_to(self.out).as_posix(), doc['candidates'])
        for key in ('scanned', 'metadataBytes', 'retainedIndexes', 'preparationProgressValid'):
            self.assertIn(key, doc)
        self.assertTrue(orphan.exists())

    def test_cli_explicit_execute_respects_limits(self):
        orphans = [self.asset('objects', '.uih', f'orphan-{i}'.encode()) for i in range(3)]
        code, out, err = self.cli('--execute', '--max-files', '1', '--max-bytes', '64')
        self.assertEqual((code, err), (0, ''))
        doc = json.loads(out)
        self.assertEqual((doc['state'], doc['removed']), ('executed', 1))
        remaining = sum((self.out / 'objects' / f'{sha}.uih').exists() for sha in orphans)
        self.assertEqual(remaining, 2)

    def test_cli_invalid_limits_exit_2_without_collecting(self):
        for args in (['--max-files', '0'], ['--max-files', '129'], ['--max-files', 'x'],
                     ['--max-bytes', '0'], ['--max-bytes', str(gc.MAX_BYTES + 1)], ['--max-bytes', '1.5'],
                     ['--execute', 'yes']):
            with self.subTest(args=args):
                with patch.object(gc, 'collect') as collect, self.assertRaises(SystemExit) as raised:
                    self.cli(*args)
                self.assertEqual(raised.exception.code, 2)
                collect.assert_not_called()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            gc.main([])                                             # --out is required
        self.assertEqual(raised.exception.code, 2)

    def test_cli_errors_are_nonzero_with_one_stderr_line(self):
        orphan = self.out / 'objects' / f"{self.asset('objects', '.uih', b'orphan')}.uih"
        (self.out / 'current.json').write_bytes(b'not a pointer')
        code, out, err = self.cli('--execute')
        self.assertEqual((code, out), (1, ''))
        self.assertTrue(err.startswith('hires_gc: '))
        self.assertEqual(err.count('\n'), 1)
        self.assertTrue(orphan.exists())
        (self.out / hb.OWNED_MARKER).unlink()
        code, out, err = self.cli()
        self.assertEqual((code, out), (1, ''))
        self.assertTrue(err.startswith('hires_gc: '))

    def test_cli_busy_lock_is_a_result_not_an_error(self):
        import fcntl
        with (self.out / '.build.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out, err = self.cli('--execute')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['state'], 'producer-busy')


if __name__ == '__main__':
    unittest.main()

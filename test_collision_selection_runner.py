"""Runner contract tests; the renderer is never launched."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import collision_selection_runner as runner
import collision_export as ce


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.args = types.SimpleNamespace(world=base/'world', catalog_dir=base/'catalog',
                                          hires_store=base/'hires', out=base/'private',
                                          players=base/'players.json', max_players=1)
        for path in (self.args.world, self.args.catalog_dir, self.args.hires_store):
            path.mkdir()
        self.args.players.write_text('{}')  # malformed input leaves only pinned pilot tiles
        self.time = 10000.25
        self.lock_module = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *_: None)

    def run_with(self, child):
        with patch.dict(sys.modules, {'fcntl': self.lock_module}), patch.object(subprocess, 'run', side_effect=child):
            return runner.run(self.args, clock=lambda: self.time)

    def previous(self, attempts):
        self.args.out.mkdir(exist_ok=True)
        (self.args.out/'selection-state.json').write_text(json.dumps({'format': runner.STATE_FORMAT,
                                                                       'attempts': attempts}))

    def fake_status(self, command, *, code=0, tiles=None, stale=False):
        selected = [command[i+1] for i, word in enumerate(command) if word == '--tile']
        active = [command[i+1] for i, word in enumerate(command) if word == '--active-tile']
        status = {'format': ce.STATUS_FORMAT, 'state': 'done' if code == 0 else 'partial' if code == 2
                  else 'failed: index publication',
                  'startedAt': '1970-01-01T02:46:40+00:00', 'finishedAt': '1970-01-01T02:46:40+00:00',
                  'activeLimit': len(active), 'requested': len(selected),
                  'tiles': tiles if tiles is not None else {k: {'result': 'reused'} for k in selected},
                  'errors': []}
        (self.args.out/'status.json').write_text(json.dumps(status))
        output = dict(status)
        if stale:
            output['startedAt'] = '1970-01-01T02:45:00+00:00'
        return subprocess.CompletedProcess(command, code, json.dumps(output), '')

    def test_success_records_only_child_reported_selected_tiles(self):
        self.previous({'1,-2': 9000})
        def child(command, **_):
            selected = [command[i+1] for i, word in enumerate(command) if word == '--tile']
            return self.fake_status(command, tiles={selected[0]: {'result': 'reused'},
                                                    selected[1]: {'result': 'skipped', 'reason': 'not started: budget'}})
        report = self.run_with(child)
        self.assertEqual(report['state'], 'done')
        self.assertEqual(len(report['attempted']), 1)
        self.assertEqual(report['attempts'][report['attempted'][0]], self.time)
        self.assertEqual(report['attempts']['1,-2'], 9000)

    def test_busy_exporter_cannot_count_same_second_stale_status(self):
        self.previous({'1,-2': 9000})
        def child(command, **_):
            self.fake_status(command)  # an unrelated status can have the same second
            return subprocess.CompletedProcess(command, 3, '{"state":"skipped"}', '')
        report = self.run_with(child)
        self.assertEqual((report['state'], report['attempted'], report['attempts']), ('busy', [], {'1,-2': 9000}))

    def test_success_exit_with_stale_or_mismatched_status_is_error(self):
        for stale in (True, False):
            with self.subTest(stale=stale):
                def child(command, **_):
                    result = self.fake_status(command, stale=stale)
                    if not stale:
                        (self.args.out/'status.json').write_text('{}')
                    return result
                report = self.run_with(child)
                self.assertEqual((report['state'], report['attempted']), ('error', []))

    def test_timeout_preserves_attempts_and_reports_error(self):
        self.previous({'1,-2': 9000})
        def child(command, **_):
            self.fake_status(command)
            raise subprocess.TimeoutExpired(command, 240)
        report = self.run_with(child)
        self.assertEqual((report['state'], report['attempted'], report['attempts']),
                         ('error', [], {'1,-2': 9000}))
        self.assertIn('timed out', report['processError'])

    def test_failed_publication_records_only_valid_child_tile_outcomes(self):
        def child(command, **_):
            selected = [command[i+1] for i, word in enumerate(command) if word == '--tile']
            return self.fake_status(command, code=1, tiles={selected[0]: {'result': 'published'},
                                                            selected[1]: {'result': 'skipped', 'reason': 'budget'}})
        report = self.run_with(child)
        self.assertEqual(report['state'], 'error')
        self.assertEqual(len(report['attempted']), 1)
        self.assertEqual(report['processError'], 'failed: index publication')

    def test_corrupt_prior_state_refuses_before_launch(self):
        self.args.out.mkdir()
        (self.args.out/'selection-state.json').write_text('{bad')
        with patch.dict(sys.modules, {'fcntl': self.lock_module}), patch.object(subprocess, 'run') as child:
            with self.assertRaisesRegex(ValueError, 'unreadable'):
                runner.run(self.args, clock=lambda: self.time)
        child.assert_not_called()

    def test_out_cannot_enclose_players_input(self):
        self.args.players = self.args.out/'players.json'
        with self.assertRaises(ce.ExportError):
            self.run_with(lambda *_args, **_kw: self.fail('exporter launched'))


if __name__ == '__main__':
    unittest.main()

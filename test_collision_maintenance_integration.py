"""Integration tests for review 217 B4 (export lock compatibility) and B5 (runner with an explicit world GC profile).

POSIX only (fcntl). Real: collision_export._lock, collision_gc.default_export_lock / enable_gc / record_publication,
collision_world_gc.run_gc, collision_world_store.publish_incremental, collision_world_runner.run_pass, and small real
UIB1/UIH1 pair fixtures (test_collision_world_store.make_pair). Mocked: region staging and the source/surface check
(stage_fn / check_fn) - this isolates the orchestration; it is NOT a real-world render or a full-world export.
    python3 -m unittest test_collision_maintenance_integration -v
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import collision_export as ce
import collision_gc as legacy
import collision_world_gc as gc
import collision_world_runner as runner
import collision_world_store as publisher
from test_collision_world_store import make_pair

HERE = Path(__file__).resolve().parent
CHILD = """
import sys
from pathlib import Path
import collision_export as ce
import collision_gc as legacy
out = Path(sys.argv[2])
handle = ce._lock(out) if sys.argv[1] == 'export' else legacy.default_export_lock(out)
print('locked' if handle is not None else 'busy', flush=True)
sys.stdin.read()          # hold the lock until the parent closes stdin
"""


def gc_output(root: Path) -> Path:
    """An owned output with the world GC profile deliberately enabled (test fixture only)."""
    out = root / 'out'
    (out / 'store').mkdir(parents=True)
    (out / legacy.OWNED_MARKER).write_text('test fixture')
    (out / gc.PROFILE_MARKER).write_text('test fixture')
    lock=ce._lock(out)
    try:legacy.enable_gc(out, now=time.time())
    finally:lock.close()
    return out


@unittest.skipUnless(os.name == 'posix', 'POSIX flock required')
class LockCompatibilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = gc_output(Path(os.path.realpath(tmp.name)))

    def gc_tick(self):
        return gc.run_gc(self.out, cache=gc.RetentionProofCache(), budget=publisher.Budget(1000, 1 << 30),
                         dry_run=True)

    def test_exporter_lock_makes_world_gc_busy_and_release_lets_it_run(self):
        pair = make_pair(self.out / 'store', 0, 0)[0]
        publisher.publish_incremental(self.out / 'store', expected_index=None, updates={'0,0': pair},
                                      removals=set(), cache=publisher.PairProofCache(),
                                      budget=publisher.Budget(1000, 1 << 30))
        held = ce._lock(self.out)
        self.assertIsNotNone(held)
        try:
            self.assertEqual(self.gc_tick()['state'], 'busy')
        finally:
            held.close()
        self.assertEqual(self.gc_tick()['state'], 'dry-run', 'GC runs once the exporter released the lock')

    def test_gc_lock_blocks_the_exporter_lock(self):
        held = legacy.default_export_lock(self.out)
        self.assertIsNotNone(held)
        try:
            self.assertIsNone(ce._lock(self.out))
        finally:
            held.close()
        again = ce._lock(self.out)
        self.assertIsNotNone(again)
        again.close()

    def child(self, kind):
        import select
        process = subprocess.Popen([sys.executable, '-c', CHILD, kind, str(self.out)], cwd=HERE,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:process.kill()
            process.wait(timeout=10)
            process.stdout.close()
            if not process.stdin.closed:process.stdin.close()
        self.addCleanup(cleanup)
        self.assertTrue(select.select([process.stdout],[],[],10)[0],'child lock handshake timed out')
        line = process.stdout.readline().strip()
        return process, line

    def release(self, process):
        process.stdin.close()
        self.assertEqual(process.wait(timeout=30), 0)

    def test_mutual_exclusion_across_processes_in_both_directions(self):
        process, line = self.child('export')
        self.assertEqual(line, 'locked')
        self.assertIsNone(legacy.default_export_lock(self.out), 'GC lock refused while another process exports')
        self.assertEqual(self.gc_tick()['state'], 'busy')
        self.release(process)
        handle = legacy.default_export_lock(self.out)
        self.assertIsNotNone(handle)
        handle.close()

        process, line = self.child('gc')
        self.assertEqual(line, 'locked')
        self.assertIsNone(ce._lock(self.out), 'exporter lock refused while another process runs GC')
        self.release(process)
        handle = ce._lock(self.out)
        self.assertIsNotNone(handle)
        handle.close()


@unittest.skipUnless(os.name == 'posix', 'POSIX flock required')
class RunnerProfileTests(unittest.TestCase):
    """run_pass with allow_world_gc=True: real publisher, mocked staging and source checks (orchestration only)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(os.path.realpath(tmp.name))
        self.out = gc_output(root)
        self.store = self.out / 'store'
        self.pairs = {'0,0': make_pair(self.store, 0, 0)[0], '8,0': make_pair(self.store, 8, 0)[0]}
        self.tools = SimpleNamespace(read_index=lambda p: {'tiles': {'0,0': 'a' * 64, '8,0': 'b' * 64},
                                                           'indexSha256': 'c' * 64})
        self.world, self.hires = root / 'world', root / 'hires'
        self.journal_calls = []

    def stage(self, jx, jz, **kw):
        return dict(region=[jx, jz], ok=True, tiles={f'{x},{z}': {} for x, z in kw['tiles']})

    def check(self, report, **kw):
        return {key: self.pairs[key] for key in report['tiles']}       # stands in for accepted_updates

    def run_pass(self, **kw):
        lock=ce._lock(self.out)
        self.assertIsNotNone(lock)
        try:
            return runner.run_pass(out=self.out, world=self.world, hires_store=self.hires, tools=self.tools,
                                   stage_fn=self.stage, check_fn=self.check, disk_free=lambda p: 10 ** 15, **kw)
        finally:lock.close()

    def recording(self, fail_on=None):
        original = legacy.record_publication

        def record(out, old, new, now):
            self.journal_calls.append(dict(old=old, new=new, pointer=publisher._read_pointer(self.store)))
            if fail_on is not None and len(self.journal_calls) == fail_on:
                raise OSError('injected journal failure')
            return original(out, old, new, now)
        return mock.patch.object(legacy, 'record_publication', side_effect=record)

    def test_explicit_profile_journals_old_and_new_index_before_each_pointer_swap(self):
        with self.recording():
            status = self.run_pass(allow_world_gc=True)
        self.assertEqual((status['state'], status['completedRegions'], status['failedRegions']), ('complete', 2, 0))
        first, second = self.journal_calls
        self.assertEqual((first['old'], first['pointer']), (None, None), 'first publication: no previous pointer')
        self.assertEqual((second['old'], second['pointer']), (first['new'], first['new']),
                         'journal written while the pointer still named the previous index')
        final = publisher._read_pointer(self.store)
        self.assertEqual(final, second['new'])
        journal = legacy.load_journal(self.out, self.store)['indexes']
        self.assertIn(first['new'], journal)
        self.assertIn(second['new'], journal)
        index = json.loads((self.store / 'indexes' / f'{final}.json').read_bytes())
        self.assertEqual(index['pairs'], self.pairs)

    def test_journal_failure_never_advances_the_pointer(self):
        with self.recording(fail_on=2):
            status = self.run_pass(allow_world_gc=True)
        self.assertEqual((status['state'], status['completedRegions'], status['failedRegions']), ('partial', 1, 1))
        self.assertIn('journal', status['errors'][0]['message'])
        first, second = self.journal_calls
        self.assertEqual(publisher._read_pointer(self.store), first['new'], 'pointer stays at the journaled index')
        self.assertNotIn(second['new'], legacy.load_journal(self.out, self.store)['indexes'])

    def test_enabled_profile_without_explicit_flag_is_refused_before_publishing(self):
        with self.recording():
            with self.assertRaisesRegex(ValueError, 'explicit'):
                self.run_pass()
        self.assertEqual(self.journal_calls, [])
        self.assertIsNone(publisher._read_pointer(self.store))


if __name__ == '__main__':
    unittest.main()

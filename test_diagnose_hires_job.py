"""Tests for diagnose_hires_job.py: diagnostics backfill of one clean, current job without geometry rendering.
Region reads, tile volumes and source fingerprints are injected; catalog and volume fixtures come from
test_offline_renderer (unmodified). Stdlib only.
    python -m unittest test_diagnose_hires_job -v      (from work/uie-map)
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import diagnose_hires_job as dhj
import offline_mesher
from offline_catalog import Catalog
from test_offline_renderer import ASSETS, LAYOUT, REGISTRY, volume
import world_hires as wh

MIB = 1024 * 1024


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BackfillHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.world, self.catalog_dir, self.hires, self.scratch = base / 'world', base / 'catalog', base / 'hires', base / 'scratch'
        (self.world / 'region').mkdir(parents=True)
        for name in ('r.0.0.mca', 'r.1.0.mca'):                 # planned jobs (0,0) and (1,0)
            (self.world / 'region' / name).write_bytes(b'r' * 2000)
        (self.world / 'level.dat').write_bytes(b'level')
        self.catalog_dir.mkdir()
        (self.catalog_dir / 'atlas.png').write_bytes(b'atlas')
        for sub in ('jobs', 'tiles'):
            (self.hires / sub).mkdir(parents=True)
        (self.hires / 'atlas.png').write_bytes(b'atlas')
        (self.hires / 'tiles' / '0_0.uih').write_bytes(b'geometry')
        self.catalog = Catalog(copy.deepcopy(ASSETS), copy.deepcopy(LAYOUT))
        self.catalog.identity = 'catalog-1'
        self.identity = dhj.current_identity(self.catalog)
        self.index = {'format': 'UIE_HIRES_INDEX1', 'tileSize': 64, 'renderer': 'uie-offline-mesher/1+hires',
                      'identity': self.identity, 'caves': False, 'atlas': 'atlas.png', 'atlasSha256': sha(b'atlas'),
                      'jobs': 1, 'chunks': 3, 'tiles': {'0,0': 'abcdefabcdef'}}
        (self.hires / 'index.json').write_text(json.dumps(self.index), encoding='utf-8')
        self.source = 'src'
        self.record = {'job': [0, 0], 'fingerprint': sha((self.identity + 'src').encode()),
                       'tiles': {'0,0': 'abcdefabcdef'}, 'chunks': 3, 'quads': 7, 'bytes': 8, 'fallback': 0,
                       'errors': [], 'seconds': 1.5}
        self.write_record(self.record)
        self.bad = set()
        self.copies = []
        self.on_volume = None
        self.payload = 'payload-1'                # live chunk payload digest
        self.copy_payload = None                  # digest of the private copies (None: same as live)
        self.live_registry = dict(REGISTRY)       # what level.dat holds now

    def job_path(self):
        return self.hires / 'jobs' / '0_0.json'

    def write_record(self, record):
        self.job_path().write_text(json.dumps(record, separators=(',', ':')), encoding='utf-8')

    def snapshot(self):
        """Every geometry/index/atlas byte the backfill must never change."""
        return {p.relative_to(self.hires).as_posix(): p.read_bytes() for p in self.hires.rglob('*')
                if p.is_file() and p.parent.name != 'jobs' and p.name not in ('diagnostics.json', '.build.lock')}

    # injected world access
    def fingerprint(self, _region_dir, _area):
        return self.source                                   # region-header fingerprint (old job guard)

    def content(self, region_dir, _area):
        """Payload digest: live regions, or the private copies (scratch '.diagnose-*' directory)."""
        if '.diagnose-' in Path(region_dir).name:
            return self.copy_payload if self.copy_payload is not None else self.payload
        return self.payload

    def copy_regions(self, region_dir, scratch, area, **kw):
        self.copies.append(kw)
        return {}

    def read_chunks(self, _copies, strip, failed=None):
        if strip.z != -wh.HALO:
            return {}
        failed.update(self.bad)
        return {(cx, cz): object() for cx in range(4) for cz in range(4)}          # core chunks of tile 0,0

    def tile_volume(self, _t_area, t_core, _window):
        if self.on_volume:
            self.on_volume()
        return volume({(1, 64, 1): (700, 0), (2, 64, 1): (1, 0)}, x0=t_core.x - 16, z0=t_core.z - 16, size=96,
                      core=(t_core.x, t_core.z, 64, 64))

    def run_backfill(self, job=(0, 0), real_copy=False, **kw):
        patches = [patch.object(dhj.wh, 'fingerprint', side_effect=self.fingerprint),
                   patch.object(dhj.ce, 'content_fingerprint', side_effect=self.content),
                   patch.object(dhj.wh, 'read_chunks', side_effect=self.read_chunks),
                   patch.object(dhj, '_tile_volume', side_effect=self.tile_volume),
                   patch.object(offline_mesher.Mesher, 'mesh_batch', side_effect=AssertionError('mesher called'))]
        if not real_copy:
            patches.append(patch.object(dhj.ce, 'copy_regions_bounded', side_effect=self.copy_regions))
        options = dict(world=self.world, catalog_dir=self.catalog_dir, hires_store=self.hires,
                       scratch_out=self.scratch, catalog=self.catalog, registry=dict(REGISTRY),
                       disk_free=lambda _p: 10 ** 13, load_registry=lambda _world: self.live_registry)
        options.update(kw)
        for p in patches:
            p.start()
        try:
            return dhj.backfill_job(*job, **options)
        finally:
            for p in reversed(patches):
                p.stop()


class BackfillTests(BackfillHarness):
    def test_clean_current_job_gets_diagnostics_without_any_geometry_change(self):
        before = self.snapshot()
        result = self.run_backfill()
        self.assertEqual((result['state'], result['coveredTiles'], result['failedTiles']), ('written', 64, 0))
        self.assertLessEqual(result['startedAt'], result['finishedAt'])
        record = json.loads(self.job_path().read_text(encoding='utf-8'))
        for key, value in self.record.items():
            self.assertEqual(record[key], value, f'{key} preserved')
        diag = record['diagnostics']
        self.assertEqual((diag['scope'], diag['coverage']['tiles'], diag['coverage']['complete']), ('job', 64, True))
        mystery = next(e for e in diag['entries'] if e['id'] == 700)
        self.assertEqual((mystery['all'], mystery['visible']), (1, None), 'no mesher: visible unknown')
        self.assertFalse(diag['coverage']['visibleKnown'])
        source = record['diagnosticsSource']
        self.assertEqual(source['mode'], 'backfill')
        self.assertEqual(source['headerGuard']['sourceFingerprint'], self.record['fingerprint'])
        self.assertIn('not proof', source['headerGuard']['meaning'])
        self.assertTrue(source['historicalGeometryEquivalence'].startswith('unknown'))
        self.assertIn('current observed stable world data', source['basis'])
        self.assertEqual((source['contentSha256'], result['contentSha256']), ('payload-1', 'payload-1'))
        self.assertEqual(source['registrySha256'], dhj.ce.registry_sha256(REGISTRY))
        self.assertTrue(result['worldRefreshed'])
        self.assertEqual(self.snapshot(), before, 'no tile, index or atlas byte changed')
        self.assertEqual(self.copies[0]['budget_bytes'], 512 * MIB)
        self.assertEqual(self.copies[0]['min_free_bytes'], 20 * 1024 * MIB)
        self.assertEqual(self.copies[0]['out'], self.scratch.resolve())
        self.assertEqual(list(self.scratch.iterdir()), [], 'scratch removed')
        world = json.loads((self.hires / 'diagnostics.json').read_text(encoding='utf-8'))
        self.assertEqual(world['jobs'], {'planned': 2, 'withDiagnostics': 1, 'withoutDiagnostics': 1, 'completeJobs': 1})
        self.assertFalse(world['coverage']['complete'], 'job 1,0 has no record: unknown, not zero')
        self.assertTrue(all(e['lowerBound'] for e in world['entries']))
        self.assertEqual(result['world'], world['jobs'])
        again = self.run_backfill()
        self.assertEqual(again['state'], 'skipped')

    def test_skip_repairs_a_missing_or_corrupt_world_summary(self):
        self.assertEqual(self.run_backfill()['state'], 'written')
        summary = self.hires / 'diagnostics.json'
        good = json.loads(summary.read_text(encoding='utf-8'))['jobs']
        for damage in ('delete', 'corrupt'):
            with self.subTest(damage):
                if damage == 'delete':
                    summary.unlink()
                else:
                    summary.write_text('{"broken', encoding='utf-8')
                raw = self.job_path().read_bytes()
                result = self.run_backfill()
                self.assertEqual(result['state'], 'skipped')
                self.assertTrue(result['worldRefreshed'])
                self.assertEqual(json.loads(summary.read_text(encoding='utf-8'))['jobs'], good)
                self.assertEqual(self.job_path().read_bytes(), raw, 'a skip never rewrites the job record')
        with patch.object(dhj, 'refresh_world_diagnostics', side_effect=OSError('disk full')):
            failed = self.run_backfill()
        self.assertEqual(failed['state'], 'skipped')
        self.assertFalse(failed['worldRefreshed'])
        self.assertIn('disk full', failed['worldError'], 'the summary failure is reported, not hidden')

    def test_same_header_payload_change_during_the_backfill_is_refused(self):
        raw = self.job_path().read_bytes()
        def save_with_same_header():
            self.payload = 'payload-2'           # region headers (and the job fingerprint) unchanged
        self.on_volume = save_with_same_header
        result = self.run_backfill()
        self.assertEqual(result['state'], 'refused')
        self.assertIn('chunk payloads', result['reason'])
        self.assertEqual(self.job_path().read_bytes(), raw)
        self.assertFalse((self.hires / 'diagnostics.json').exists())

    def test_copies_that_differ_from_the_live_source_are_refused(self):
        self.copy_payload = 'payload-other'
        result = self.run_backfill()
        self.assertEqual(result['state'], 'refused')
        self.assertIn('private copies differ', result['reason'])
        self.assertNotIn('diagnostics', json.loads(self.job_path().read_text(encoding='utf-8')))

    def test_registry_mismatch_before_or_change_during_is_refused(self):
        self.live_registry = {**REGISTRY, 950: 'mod:new_block'}
        before = self.run_backfill()
        self.assertEqual(before['state'], 'refused')
        self.assertIn('differs from the registry loaded', before['reason'])
        self.assertEqual(self.copies, [], 'refused before any copy')
        self.live_registry = dict(REGISTRY)
        def registry_changes():
            self.live_registry = {**REGISTRY, 951: 'mod:other'}
        self.on_volume = registry_changes
        during = self.run_backfill()
        self.assertEqual(during['state'], 'refused')
        self.assertIn('registry changed during', during['reason'])
        self.assertNotIn('diagnostics', json.loads(self.job_path().read_text(encoding='utf-8')))

    def test_source_change_during_the_backfill_writes_nothing(self):
        raw = self.job_path().read_bytes()
        def change():
            self.source = 'src2'
        self.on_volume = change
        result = self.run_backfill()
        self.assertEqual(result['state'], 'refused')
        self.assertIn('source changed', result['reason'])
        self.assertEqual(self.job_path().read_bytes(), raw)
        self.assertFalse((self.hires / 'diagnostics.json').exists())

    def test_record_changed_during_the_backfill_is_not_overwritten(self):
        changed = dict(self.record, seconds=9.9)
        self.on_volume = lambda: self.write_record(changed)
        result = self.run_backfill()
        self.assertEqual(result['state'], 'refused')
        self.assertEqual(json.loads(self.job_path().read_text(encoding='utf-8')), changed)

    def test_only_clean_current_jobs_of_this_renderer_are_augmented(self):
        cases = {
            'stale fingerprint': lambda: self.write_record(dict(self.record, fingerprint='f' * 64)),
            'tile errors': lambda: self.write_record(dict(self.record, errors=[[0, 0, 'mesh failed']])),
            'other renderer': lambda: (self.hires / 'index.json').write_text(
                json.dumps(dict(self.index, identity='other')), encoding='utf-8'),
            'other atlas': lambda: (self.hires / 'atlas.png').write_bytes(b'other atlas'),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self.setUp()
                mutate()
                raw = self.job_path().read_bytes()
                result = self.run_backfill()
                self.assertEqual(result['state'], 'refused', label)
                self.assertEqual(self.copies, [], 'refused before any copy')
                self.assertEqual(self.job_path().read_bytes(), raw)
        self.setUp()
        self.assertIn('not planned', self.run_backfill(job=(5, 5))['reason'])

    def test_unreadable_halo_fails_the_tile_and_a_partial_result_stays_incomplete(self):
        self.bad = {(-1, 0)}                     # halo chunk of tile 0,0 only
        result = self.run_backfill()
        self.assertEqual((result['state'], result['coveredTiles'], result['failedTiles']), ('partial', 63, 1))
        record = json.loads(self.job_path().read_text(encoding='utf-8'))
        self.assertFalse(record['diagnostics']['coverage']['complete'])
        self.assertEqual(record['diagnosticErrors'][0][:2], [0, 0])
        self.assertIn('source read failed', record['diagnosticErrors'][0][2])
        self.assertFalse(any(e['id'] == 700 for e in record['diagnostics']['entries']), 'failed tile is not air or zero')
        self.bad = set()
        retry = self.run_backfill()
        self.assertEqual(retry['state'], 'written', 'incomplete diagnostics are not skipped')
        self.assertNotIn('diagnosticErrors', json.loads(self.job_path().read_text(encoding='utf-8')))

    def test_copy_budget_and_paths_are_guarded(self):
        raw = self.job_path().read_bytes()
        result = self.run_backfill(real_copy=True, budget_bytes=100)
        self.assertEqual(result['state'], 'refused')
        self.assertIn('budget', result['reason'])
        self.assertEqual(self.job_path().read_bytes(), raw)
        self.assertEqual([p for p in self.scratch.iterdir()] if self.scratch.exists() else [], [])
        inside = self.run_backfill(scratch_out=self.hires / 'scratch')
        self.assertEqual(inside['state'], 'refused')
        self.assertFalse((self.hires / 'scratch').exists())

    def test_identity_formula_matches_world_hires_build(self):
        import multiprocessing
        import offline_catalog
        import reader
        out = Path(self._tmp.name) / 'build-out'
        (self.catalog_dir / 'atlas-layout.json').write_text('{}', encoding='utf-8')

        class Pool:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def imap_unordered(self, _w, jobs): return iter([])
        with (patch.object(wh, 'plan_jobs', return_value=[]),
              patch.object(reader, 'parse_level_dat', return_value=None),
              patch.object(reader, 'parse_fml_registry', return_value=SimpleNamespace(blocks={})),
              patch.object(offline_catalog.Catalog, 'load_compiled', return_value=SimpleNamespace(identity='catalog-1')),
              patch.object(multiprocessing, 'get_context', return_value=SimpleNamespace(Pool=Pool)),
              patch.object(wh.shutil, 'disk_usage', return_value=SimpleNamespace(free=wh.MIN_FREE_BYTES + 1))):
            wh.build(self.world, self.catalog_dir, out, workers=1)
        index = json.loads((out / 'index.json').read_text(encoding='utf-8'))
        self.assertEqual(index['identity'], self.identity, 'same formula as world_hires.build')

    @unittest.skipUnless(hasattr(__import__('os'), 'fork'), 'fcntl lock semantics are Linux-only')
    def test_busy_build_lock_exits_3_without_work(self):
        import fcntl
        with open(self.hires / '.build.lock', 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(dhj.os, 'nice'):
                code = dhj.main(['--job', '0,0', '--world', str(self.world), '--catalog-dir', str(self.catalog_dir),
                                 '--hires-store', str(self.hires), '--scratch-out', str(self.scratch)])
        self.assertEqual(code, 3)
        self.assertNotIn('diagnostics', json.loads(self.job_path().read_text(encoding='utf-8')))

    def test_cli_refuses_bad_job_syntax_and_overlapping_paths(self):
        common = ['--world', str(self.world), '--catalog-dir', str(self.catalog_dir), '--hires-store', str(self.hires)]
        with patch.object(dhj.os, 'nice', create=True):
            self.assertEqual(dhj.main(['--job', '01,0', *common, '--scratch-out', str(self.scratch)]), 1)
            self.assertEqual(dhj.main(['--job', '0,0', *common, '--scratch-out', str(self.world / 's')]), 1)
        with self.assertRaises(SystemExit):
            dhj.main(common + ['--scratch-out', str(self.scratch)])                      # --job is required


if __name__ == '__main__':
    unittest.main()

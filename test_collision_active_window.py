"""Explicit bounded collision index selection. No GC or production store involved."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import collision_export as ce
from test_collision_export import ExportHarness, geometry_sha12


class ActiveWindowTests(ExportHarness):
    def test_old_two_tiles_to_new_subset_without_verifying_inactive_pair(self):
        self.run_export(['-1,-1', '0,0'])
        prior = self.index_pairs()['-1,-1']
        checked = []
        original = ce.verify_pair_objects

        def verify(store, key, sha):
            checked.append(key)
            return original(store, key, sha)

        with patch.object(ce, 'verify_pair_objects', side_effect=verify):
            status = self.run_export(['0,0'], active_keys={'0,0'})
        self.assertEqual(status['state'], 'done')
        self.assertEqual(status['activeLimit'], 1)
        self.assertEqual(status['evicted'], 1)
        self.assertEqual(status['missingActive'], [])
        self.assertEqual(set(self.index_pairs()), {'0,0'})
        self.assertNotIn('-1,-1', checked, 'inactive old pair should not be decoded merely to evict it')
        self.assertTrue((self.out / 'store' / 'pairs' / f'{prior}.json').exists(), 'objects are not deleted')

    def test_invalid_window_or_selection_is_rejected_before_any_output_write(self):
        for active in (set(), {'not-a-key'}, {'0,0'}, {f'{n},0' for n in range(33)}):
            with self.subTest(active=active):
                with self.assertRaises(ce.ExportError):
                    self.run_export(['-1,-1'], active_keys=active)
                self.assertFalse(self.out.exists())
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'world').mkdir()
            common = ['--world', str(base / 'world'), '--catalog-dir', str(base / 'c'),
                      '--hires-store', str(base / 'h'), '--out', str(base / 'out'), '--tile', '0,0']
            self.assertEqual(ce.main(common + ['--active-tile', '1,1']), 1)
            self.assertFalse((base / 'out').exists(), 'CLI also validates before creating the output')

    def test_default_legacy_merge_keeps_unselected_previous_pair(self):
        self.run_export(['-1,-1', '0,0'])
        status = self.run_export(['0,0'])
        self.assertEqual(set(self.index_pairs()), {'-1,-1', '0,0'})
        self.assertIsNone(status['activeLimit'])
        self.assertEqual((status['evicted'], status['missingActive']), (0, []))

    def test_missing_or_stale_active_tile_is_unknown_not_air_or_done(self):
        self.write_hires({'0,0': geometry_sha12(*self.tiles['0,0'])})
        status = self.run_export(['0,0'], active_keys={'0,0', '-1,-1'})
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(status['missingActive'], ['-1,-1'])
        self.assertNotIn('-1,-1', self.index_pairs())
        # A formerly valid active pair becomes stale under a newer visible geometry.
        self.write_hires({'0,0': 'f' * 12})
        status = self.run_export(['0,0'], active_keys={'0,0'})
        self.assertEqual(status['missingActive'], ['0,0'])
        self.assertEqual(self.index_pairs(), {})


class ActiveProvenanceTests(ExportHarness):
    def test_removed_index_key_is_pruned_from_provenance(self):
        revisions = {'-1,-1': '1' * 64, '0,0': '2' * 64}
        export = lambda keys, **kw: self.run_export(keys, source_revision=lambda tx, tz: revisions[f'{tx},{tz}'], **kw)
        export(['-1,-1', '0,0'])
        removed_pair = self.index_pairs()['-1,-1']
        status = export(['0,0'], active_keys={'0,0'})
        self.assertEqual(status['state'], 'done')
        self.assertEqual(status['evicted'], 1)
        self.assertEqual(set(self.index_pairs()), {'0,0'})
        provenance = json.loads((self.out / 'provenance.json').read_bytes())['tiles']
        self.assertEqual(set(provenance), {'0,0'})
        self.assertTrue((self.out / 'store' / 'pairs' / f'{removed_pair}.json').exists())


if __name__ == '__main__':
    unittest.main()

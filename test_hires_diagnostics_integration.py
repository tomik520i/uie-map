import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import world_hires as wh
import world_diagnostics as wd


class IntegrationTests(unittest.TestCase):
    def test_sidecar_keeps_unknown_old_jobs_and_does_not_change_geometry_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            meta = {'identity': 'renderer-same', 'atlasSha256': 'a' * 64}
            records = {(0, 0): {'tiles': {'0,0': 'b' * 12}, 'chunks': 16},
                       (1, 0): {'tiles': {}, 'chunks': 0}}
            wh._write_index(out, meta, records, planned=[(0, 0), (1, 0)])
            before = (out / 'index.json').read_bytes()
            records[(0, 0)]['diagnostics'] = wd.job_diagnostics([wd.empty_tile_diagnostics()], 64)
            wh._write_index(out, meta, records, planned=[(0, 0), (1, 0)])
            self.assertEqual((out / 'index.json').read_bytes(), before)
            diag = json.loads((out / 'diagnostics.json').read_bytes())
            self.assertEqual(diag['jobs'], {'planned': 2, 'withDiagnostics': 1, 'withoutDiagnostics': 1, 'completeJobs': 0})
            self.assertFalse(diag['coverage']['complete'])
            self.assertTrue(diag['classes']['fallback']['lowerBound'])
            self.assertEqual(diag['rendererIdentity'], meta['identity'])

    def test_empty_successful_tiles_are_covered_without_mesh(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp); (out / 'tiles').mkdir()
            record = {'tiles': {}, 'chunks': 0, 'quads': 0, 'bytes': 0, 'fallback': 0, 'errors': []}
            area, core = wh.tile_area(0, 0)
            with patch.object(wh, 'read_chunks', return_value={}):
                wh._render_rows(record, {}, area, core, out, SimpleNamespace(), lambda _: self.fail('No volume for absent chunks'))
            self.assertEqual(record['diagnostics']['coverage']['tiles'], 1)
            self.assertTrue(record['diagnostics']['coverage']['complete'])
            self.assertEqual(record['errors'], [])


if __name__ == '__main__':
    unittest.main()

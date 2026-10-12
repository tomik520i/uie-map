import os
from pathlib import Path
import tempfile
import unittest
from detail_storage import inspect_runs, require_run_capacity, DetailStorageError


class DetailStorageTests(unittest.TestCase):
    def test_missing_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            state=Path(d)/'missing'
            self.assertEqual(inspect_runs(state)['logicalBytes'],0)
            self.assertFalse(state.exists())

    def test_budget_refuses_without_modifying_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);run=root/'runs'/'a';run.mkdir(parents=True)
            artifact=run/'snapshot';artifact.write_bytes(b'12345')
            self.assertEqual(require_run_capacity(root,budget=6)['files'],1)
            with self.assertRaises(DetailStorageError):require_run_capacity(root,budget=5)
            self.assertEqual(artifact.read_bytes(),b'12345')

    def test_entry_limit_counts_directories(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'runs'/'a'/'b').mkdir(parents=True)
            with self.assertRaises(DetailStorageError):inspect_runs(root,max_entries=1)

    def test_hardlinks_conservative_not_free_space_claim(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);run=root/'runs';run.mkdir()
            (run/'a').write_bytes(b'123');os.link(run/'a',run/'b')
            self.assertEqual(inspect_runs(root)['logicalBytes'],6)

    def test_symlink_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'runs').mkdir();(root/'source').write_text('keep')
            try:(root/'runs'/'link').symlink_to(root/'source')
            except OSError:self.skipTest('symlink privilege unavailable')
            with self.assertRaises(DetailStorageError):inspect_runs(root)
            self.assertEqual((root/'source').read_text(),'keep')

    def test_invalid_limits(self):
        for limit in [0,True,100001,1.5]:
            with self.assertRaises(DetailStorageError):inspect_runs('.',max_entries=limit)


if __name__=='__main__':unittest.main()

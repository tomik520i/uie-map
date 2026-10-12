import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import collision_gc as gc
import collision_maintenance as cm


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name).resolve()
        (self.out/'store').mkdir()
        (self.out/gc.OWNED_MARKER).write_text('test')

    def test_refused_is_reported_without_enabling_output(self):
        with patch.object(gc, 'run_gc', return_value={'state': 'refused', 'reason': 'not enabled'}):
            self.assertEqual(cm.main(['--out', str(self.out)]), 1)
        self.assertFalse((self.out/gc.ENABLED_MARKER).exists())
        report = json.loads((self.out/'gc-status.json').read_text())
        self.assertEqual(report['result']['state'], 'refused')

    def test_busy_preserves_last_useful_status(self):
        path = self.out/'gc-status.json'
        path.write_text('previous')
        with patch.object(gc, 'run_gc', return_value={'state': 'busy'}):
            self.assertEqual(cm.main(['--out', str(self.out)]), 0)
        self.assertEqual(path.read_text(), 'previous')

    def test_unowned_output_is_never_touched(self):
        (self.out/gc.OWNED_MARKER).unlink()
        with patch.object(gc, 'run_gc') as run:
            self.assertEqual(cm.main(['--out', str(self.out)]), 1)
            run.assert_not_called()
        self.assertFalse((self.out/'gc-status.json').exists())


if __name__ == '__main__':
    unittest.main()

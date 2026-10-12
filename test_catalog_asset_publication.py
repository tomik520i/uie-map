import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import world_hires as wh


class CatalogAssetPublicationTests(unittest.TestCase):
    def test_replacement_keeps_published_hardlink_and_unchanged_inode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, target, old = [root / name for name in ('source', 'atlas.png', 'old.png')]
            source.write_bytes(b'old atlas')
            wh._publish_catalog_asset(source, target)
            os.link(target, old)
            before = target.stat().st_ino
            wh._publish_catalog_asset(source, target)
            self.assertEqual(target.stat().st_ino, before)
            source.write_bytes(b'new atlas')
            wh._publish_catalog_asset(source, target)
            self.assertEqual(target.read_bytes(), b'new atlas')
            self.assertEqual(old.read_bytes(), b'old atlas')
            self.assertNotEqual(target.stat().st_ino, old.stat().st_ino)

    def test_failed_replace_keeps_old_asset_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, target = root / 'source', root / 'atlas.png'
            source.write_bytes(b'new')
            target.write_bytes(b'old')
            with patch.object(wh.os, 'replace', side_effect=OSError('injected replace failure')):
                with self.assertRaises(OSError):
                    wh._publish_catalog_asset(source, target)
            self.assertEqual(target.read_bytes(), b'old')
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['atlas.png', 'source'])


if __name__ == '__main__':
    unittest.main()

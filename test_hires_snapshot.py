"""Tests for hires_snapshot.py (private legacy -> immutable snapshot adapter). Stdlib only; never touches a live store.
    python -m unittest test_hires_snapshot -v      (from work/uie-map)
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hires_bundle as hb
import hires_snapshot as hs
import world_hires as wh
from test_world_hires import quad

QUADS = {'0,0': quad(1, 64, 1), '1,0': quad(65, 64, 1), '-1,-1': quad(-63, 64, -63)}
IDENTITY = hashlib.sha256(b'renderer').hexdigest()


def tile_bytes(key, shift=0):
    tx, tz = (int(v) for v in key.split(','))
    part = quad(*(c + shift if i != 1 else c for i, c in enumerate(QUADS[key]['positions'][:3])))
    return wh.encode_tile(tx * wh.TILE, tz * wh.TILE, [part])


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name).resolve() / 'hires'
        (self.out / 'tiles').mkdir(parents=True)
        (self.out / hb.OWNED_MARKER).write_text('private test')
        self.atlas = hb.PNG_SIGNATURE + b'\x00' * 40
        (self.out / 'atlas.png').write_bytes(self.atlas)
        self.meta = {'renderer': 'uie-offline-mesher/1+hires', 'identity': IDENTITY, 'caves': False,
                     'atlas': 'atlas.png', 'atlasSha256': hashlib.sha256(self.atlas).hexdigest()}
        refs = {}
        for key in QUADS:
            data = tile_bytes(key)
            self.alias(key).write_bytes(data)
            refs[key] = hashlib.sha256(data).hexdigest()[:12]
        # The real legacy writer of world_hires (index.json + diagnostics.json), one record without diagnostics.
        wh._write_index(self.out, self.meta, {(0, 0): {'tiles': refs, 'chunks': 3}})
        self.refs = refs

    def alias(self, key):
        return self.out / 'tiles' / f"{key.replace(',', '_')}.uih"

    def index(self):
        return json.loads((self.out / 'index.json').read_bytes())

    def write_index(self, doc):
        (self.out / 'index.json').write_text(json.dumps(doc), encoding='utf-8')

    def pointer(self):
        path = self.out / 'current.json'
        return path.read_bytes() if path.exists() else None

    def published(self):
        result = hs.snapshot(self.out)
        return result, self.pointer()

    def assert_refused(self, pointer, **kw):
        with self.assertRaises(hb.BundleError):
            hs.snapshot(self.out, **kw)
        self.assertEqual(self.pointer(), pointer)

    def assert_nothing_linked(self):
        self.assertFalse((self.out / 'objects').exists())
        self.assertFalse((self.out / 'atlases').exists())

    def test_valid_legacy_output_is_published_through_hardlinks(self):
        result, pointer = self.published()
        self.assertEqual((result['format'], result['private'], result['note']),
                         (hs.RESULT_FORMAT, True, 'private adapter, no automatic integration'))
        self.assertEqual(result['tiles'], 3)
        sizes = sum(self.alias(k).stat().st_size for k in QUADS) + len(self.atlas)
        self.assertEqual((result['sourceBytes'], result['preflightBytes']), (sizes, sizes))
        self.assertEqual(result['legacyIndexSha256'], hashlib.sha256((self.out / 'index.json').read_bytes()).hexdigest())
        self.assertEqual((result['rendererIdentity'], result['renderer'], result['caves']),
                         (IDENTITY, 'uie-offline-mesher/1+hires', False))
        self.assertEqual(json.loads(pointer), {'format': hb.POINTER_FORMAT, 'indexSha256': result['indexSha256']})
        raw = (self.out / 'indexes' / f"{result['indexSha256']}.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['indexSha256'])
        doc = json.loads(raw)
        self.assertEqual((doc['atlasSha256'], doc['rendererIdentity']), (self.meta['atlasSha256'], IDENTITY))
        self.assertEqual(set(doc['tiles']), set(QUADS))
        for key, sha in doc['tiles'].items():
            self.assertEqual(sha[:12], self.refs[key])
            self.assertEqual(os.stat(self.out / 'objects' / f'{sha}.uih').st_ino, self.alias(key).stat().st_ino)
        self.assertEqual(os.stat(self.out / 'atlases' / f"{doc['atlasSha256']}.png").st_ino,
                         (self.out / 'atlas.png').stat().st_ino)
        self.assertEqual(hs.snapshot(self.out)['indexSha256'], result['indexSha256'], 'repeatable, same index')

    def test_wrong_sha12_refuses_and_keeps_previous_pointer(self):
        _, pointer = self.published()
        replacement = self.out / 'tiles' / 'new.uih'
        replacement.write_bytes(tile_bytes('1,0', shift=1))
        os.replace(replacement, self.alias('1,0'))
        with self.assertRaisesRegex(hs.SnapshotError, 'does not match its legacy sha12'):
            hs.snapshot(self.out)
        self.assertEqual(self.pointer(), pointer)

    def test_atlas_not_matching_the_index_refuses(self):
        _, pointer = self.published()
        replacement = self.out / 'atlas.new'
        replacement.write_bytes(self.atlas + b'\x01')
        os.replace(replacement, self.out / 'atlas.png')
        with self.assertRaisesRegex(hs.SnapshotError, 'atlasSha256'):
            hs.snapshot(self.out)
        self.assertEqual(self.pointer(), pointer)

    def test_source_replaced_during_the_snapshot_refuses(self):
        _, pointer = self.published()
        real = hb.ingest_tile
        def swap(out, source, key):
            if key == '1,0':
                replacement = self.out / 'tiles' / 'swap.uih'
                replacement.write_bytes(tile_bytes(key, shift=2))
                os.replace(replacement, source)
            return real(out, source, key)
        with patch.object(hb, 'ingest_tile', side_effect=swap):
            with self.assertRaisesRegex(hs.SnapshotError, 'legacy sha12'):
                hs.snapshot(self.out)
        self.assertEqual(self.pointer(), pointer)

    def test_index_replaced_during_the_snapshot_refuses(self):
        _, pointer = self.published()
        real = hb.ingest_tile
        original = (self.out / 'index.json').read_bytes()
        def republish(out, source, key):
            replacement = self.out / 'index.new'
            replacement.write_bytes(original + b' ')        # same document, different bytes
            os.replace(replacement, self.out / 'index.json')
            return real(out, source, key)
        with patch.object(hb, 'ingest_tile', side_effect=republish):
            with self.assertRaisesRegex(hs.SnapshotError, 'changed during the snapshot'):
                hs.snapshot(self.out)
        self.assertEqual(self.pointer(), pointer)

    def test_budgets_refuse_before_any_link(self):
        self.assert_refused(None, max_tiles=2)
        self.assert_refused(None, max_source_bytes=len(self.atlas) + 10)
        self.assert_nothing_linked()
        for kw in ({'max_tiles': 0}, {'max_tiles': hs.HARD_MAX_TILES + 1}, {'max_tiles': True},
                   {'max_tiles': 3.0}, {'max_source_bytes': 0}, {'max_source_bytes': hs.HARD_MAX_SOURCE_BYTES + 1}):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    hs.snapshot(self.out, **kw)
        self.assert_nothing_linked()

    def test_whole_world_sized_index_is_refused_by_the_preflight(self):
        doc = self.index()
        doc['tiles'] = {f'{tx},{tz}': '0' * 12 for tx in range(-120, 120) for tz in range(-120, 120)}   # 57 600
        self.write_index(doc)
        with self.assertRaisesRegex(hs.SnapshotError, 'exceed the budget'):
            hs.snapshot(self.out, max_tiles=hs.HARD_MAX_TILES)
        self.assert_nothing_linked()

    def test_malformed_legacy_index_refuses_before_any_link(self):
        base = self.index()
        changes = {
            'format': lambda d: d.update(format='UIE_HIRES_INDEX2'),
            'tileSize': lambda d: d.update(tileSize=32),
            'tileSize float': lambda d: d.update(tileSize=64.0),
            'atlas name': lambda d: d.update(atlas='atlas2.png'),
            'atlas hash': lambda d: d.update(atlasSha256=d['atlasSha256'][:12]),
            'identity': lambda d: d.pop('identity'),
            'caves': lambda d: d.pop('caves'),
            'tiles list': lambda d: d.update(tiles=[]),
            'key leading zero': lambda d: d['tiles'].update({'01,0': '0' * 12}),
            'key minus zero': lambda d: d['tiles'].update({'-0,0': '0' * 12}),
            'key outside': lambda d: d['tiles'].update({'468751,0': '0' * 12}),
            'sha12 upper': lambda d: d['tiles'].update({'0,0': d['tiles']['0,0'].upper()}),
            'sha12 long': lambda d: d['tiles'].update({'0,0': d['tiles']['0,0'] + '0'}),
        }
        for label, change in changes.items():
            with self.subTest(label):
                doc = json.loads(json.dumps(base))
                change(doc)
                self.write_index(doc)
                self.assert_refused(None)
        for label, text in {'duplicate': '{"format":"UIE_HIRES_INDEX1","format":"UIE_HIRES_INDEX1"}',
                            'nan': '{"format":"UIE_HIRES_INDEX1","tileSize":NaN}', 'not json': '{',
                            'not utf8': '\udcff'}.items():
            with self.subTest(label):
                (self.out / 'index.json').write_bytes(text.encode('utf-8', 'surrogateescape'))
                self.assert_refused(None)
        self.write_index(base)
        with patch.object(hs, 'MAX_LEGACY_INDEX_BYTES', 16):
            self.assert_refused(None)
        self.assert_nothing_linked()

    def test_unexpected_missing_files_refuse(self):
        _, pointer = self.published()
        self.alias('-1,-1').unlink()
        with self.assertRaisesRegex(hs.SnapshotError, 'missing'):
            hs.snapshot(self.out)
        self.assertEqual(self.pointer(), pointer)
        for name in ('atlas.png', 'index.json'):
            with self.subTest(name):
                (self.out / name).unlink()
                self.assert_refused(pointer)

    def test_empty_map_needs_explicit_consent(self):
        doc = self.index()
        doc['tiles'] = {}
        self.write_index(doc)
        with self.assertRaisesRegex(hs.SnapshotError, 'no tiles'):
            hs.snapshot(self.out)
        self.assertIsNone(self.pointer())
        result = hs.snapshot(self.out, allow_empty=True)
        self.assertEqual((result['tiles'], result['sourceBytes']), (0, len(self.atlas)))
        self.assertEqual(json.loads(self.pointer())['indexSha256'], result['indexSha256'])

    def test_foreign_output_is_refused(self):
        (self.out / hb.OWNED_MARKER).unlink()
        with self.assertRaises((hb.BundleError, FileNotFoundError)):
            hs.snapshot(self.out)
        self.assert_nothing_linked()

    def test_empty_map_atlas_budget_refuses_before_any_link(self):
        doc = self.index()
        doc['tiles'] = {}
        self.write_index(doc)
        self.assert_refused(None, allow_empty=True, max_source_bytes=1)
        self.assert_nothing_linked()

    @unittest.skipUnless(os.name == 'posix', 'directory symlink test requires POSIX')
    def test_symlink_tile_directory_refuses_before_any_link(self):
        tiles = self.out / 'tiles'
        other = self.out / 'other'
        tiles.rename(other)
        tiles.symlink_to(other, target_is_directory=True)
        self.assert_refused(None)
        self.assert_nothing_linked()


if __name__ == '__main__':
    unittest.main()

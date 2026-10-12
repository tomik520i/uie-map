"""Tests for hires_backfill.py (private resumable backfill preparer). Stdlib only; never touches a live store.
    python -m unittest test_hires_backfill -v      (from work/uie-map)
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hires_backfill as hbf
import hires_bundle as hb
import world_hires as wh
from test_world_hires import quad

KEYS = [f'{i},0' for i in range(5)]
IDENTITY = hashlib.sha256(b'renderer').hexdigest()


def tile_bytes(key, shift=1):
    tx = int(key.split(',')[0])
    return wh.encode_tile(tx * wh.TILE, 0, [quad(tx * wh.TILE + shift, 64, 1)])


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name).resolve() / 'hires'
        (self.out / 'tiles').mkdir(parents=True)
        (self.out / hb.OWNED_MARKER).write_text('private test')
        self.atlas = hb.PNG_SIGNATURE + bytes(range(256)) + b'\x00' * 144      # clearly larger than a tile
        (self.out / 'atlas.png').write_bytes(self.atlas)
        self.meta = {'renderer': 'uie-offline-mesher/1+hires', 'identity': IDENTITY, 'caves': False,
                     'atlas': 'atlas.png', 'atlasSha256': hashlib.sha256(self.atlas).hexdigest()}
        for key in KEYS:
            self.alias(key).write_bytes(tile_bytes(key))
        self.publish_legacy()

    def alias(self, key):
        return self.out / 'tiles' / f"{key.replace(',', '_')}.uih"

    def publish_legacy(self):
        """The real legacy writer of world_hires over the current aliases."""
        refs = {k: hashlib.sha256(self.alias(k).read_bytes()).hexdigest()[:12] for k in KEYS}
        wh._write_index(self.out, self.meta, {(0, 0): {'tiles': refs, 'chunks': len(refs)}})
        self.refs = refs

    def replace_alias(self, key, data):
        tmp = self.out / 'tiles' / 'replacement.tmp'
        tmp.write_bytes(data)
        os.replace(tmp, self.alias(key))

    def progress(self):
        path = self.out / hbf.PROGRESS
        return path.read_bytes() if path.exists() else None

    def assert_refused(self, pattern, **kw):
        before = self.progress()
        with self.assertRaisesRegex(hb.BundleError, pattern):
            hbf.step(self.out, **kw)
        self.assertEqual(self.progress(), before, 'a failed call never changes the progress')

    def assert_not_published(self):
        self.assertFalse((self.out / 'current.json').exists())
        self.assertFalse((self.out / 'indexes').exists())

    def test_resumable_steps_capture_everything_without_publication_or_copies(self):
        first = hbf.step(self.out, max_tiles=2)
        self.assertEqual((first['captured'], first['stepTiles'], first['remaining'], first['complete']), (2, 2, 3, False))
        self.assertIsNone(first['references'])
        self.assertEqual((first['format'], first['private']), (hbf.RESULT_FORMAT, True))
        state = json.loads(self.progress())
        self.assertEqual(state['legacyIndexSha256'], hashlib.sha256((self.out / 'index.json').read_bytes()).hexdigest())
        self.assertEqual((state['tileCount'], state['atlasLinked'], sorted(state['objects'])), (5, True, KEYS[:2]))
        self.assertEqual(hbf.step(self.out, max_tiles=2)['captured'], 4)
        last = hbf.step(self.out, max_tiles=2)
        self.assertEqual((last['captured'], last['stepTiles'], last['complete'], last['remaining']), (5, 1, True, 0))
        self.assertEqual(sorted(last['references']), sorted(KEYS))
        for key, full in last['references'].items():
            self.assertEqual(full[:12], self.refs[key])
            obj = self.out / 'objects' / f'{full}.uih'
            self.assertEqual(os.stat(obj).st_ino, self.alias(key).stat().st_ino, 'hardlink, no second data copy')
        atlas_obj = self.out / 'atlases' / f'{self.meta["atlasSha256"]}.png'
        self.assertEqual(os.stat(atlas_obj).st_ino, (self.out / 'atlas.png').stat().st_ino)
        with patch.object(hb, '_atomic', wraps=hb._atomic) as atomic:
            again = hbf.step(self.out)
            atomic.assert_not_called()
        self.assertEqual((again['complete'], again['stepTiles'], again['stepBytes']), (True, 0, 0))
        self.assertEqual(again['references'], last['references'])
        self.assert_not_published()

    def test_byte_budget_includes_the_atlas_and_bounds_every_call(self):
        sizes = {k: self.alias(k).stat().st_size for k in KEYS}
        budget = len(self.atlas) + sizes['0,0']
        first = hbf.step(self.out, max_source_bytes=budget)
        self.assertEqual((first['stepTiles'], first['stepBytes']), (1, budget), 'atlas + one tile fit, the next waits')
        while True:
            result = hbf.step(self.out, max_source_bytes=budget)
            self.assertLessEqual(result['stepBytes'], budget)
            self.assertGreater(result['stepTiles'], 0)
            if result['complete']:
                break
        self.assert_not_published()

    def test_single_item_larger_than_the_budget_is_refused_clearly(self):
        self.assert_refused('atlas.png .*exceeds the per-call byte budget', max_source_bytes=len(self.atlas) - 1)
        self.assertIsNone(self.progress())
        hbf.step(self.out, max_tiles=1)
        size = self.alias('1,0').stat().st_size
        self.assert_refused('tile 1,0 .*exceeds the per-call byte budget', max_source_bytes=size - 1)

    def test_corrupted_progress_is_never_trusted_and_restart_is_explicit(self):
        hbf.step(self.out, max_tiles=2)
        good = json.loads(self.progress())
        key, full = sorted(good['objects'].items())[0]
        def variant(**changes):
            doc = json.loads(json.dumps(good))
            for name, value in changes.items():
                doc[name] = value
            return json.dumps(doc).encode()
        variants = {
            'not json': b'not json',
            'duplicate key': b'{"format":"x","format":"y"}',
            'extra key': variant(extra=1),
            'format': variant(format='UIE_HIRES_BACKFILL_PROGRESS2'),
            'tile count': variant(tileCount=99),
            'atlasLinked type': variant(atlasLinked='yes'),
            'objects without atlas': variant(atlasLinked=False),
            'foreign key': variant(objects={**good['objects'], '9,9': full}),
            'prefix mismatch': variant(objects={**good['objects'], key: 'f' * 64}),
            'uppercase': variant(objects={**good['objects'], key: full.upper()}),
            'object missing': variant(objects={**good['objects'], key: self.refs[key] + '0' * 52}),
        }
        for label, raw in variants.items():
            with self.subTest(label):
                (self.out / hbf.PROGRESS).write_bytes(raw)
                self.assert_refused('restart=True')
        restarted = hbf.step(self.out, max_tiles=2, restart=True)
        self.assertEqual((restarted['restarted'], restarted['captured']), (True, 2))

    def test_changed_legacy_index_needs_an_explicit_restart(self):
        first = hbf.step(self.out, max_tiles=2)
        kept = json.loads(self.progress())['objects']
        self.replace_alias('4,0', tile_bytes('4,0', shift=2))
        self.publish_legacy()
        self.assert_refused('legacy index changed since this backfill started')
        result = hbf.step(self.out, max_tiles=100, restart=True)
        self.assertTrue(result['complete'])
        self.assertNotEqual(result['legacyIndexSha256'], first['legacyIndexSha256'])
        self.assertEqual(result['references']['4,0'][:12], self.refs['4,0'])
        for full in kept.values():
            self.assertTrue((self.out / 'objects' / f'{full}.uih').is_file(), 'restart never deletes objects')
        self.assert_not_published()

    def test_missing_tile_refuses_without_advancing(self):
        hbf.step(self.out, max_tiles=1)
        self.alias('1,0').unlink()
        self.assert_refused('missing')

    def test_reconcile_keeps_unchanged_tiles_and_recaptures_only_changed(self):
        first = hbf.step(self.out)
        self.replace_alias('1,0', tile_bytes('1,0', shift=3))
        self.publish_legacy()
        with patch.object(hb, 'ingest_tile', wraps=hb.ingest_tile) as ingest:
            result = hbf.step(self.out, max_tiles=1, reconcile=True)
        self.assertTrue(result['complete'])
        self.assertTrue(result['reconciled'])
        self.assertEqual(result['stepTiles'], 1)
        self.assertEqual(ingest.call_args.args[2], '1,0')
        for key in KEYS:
            if key != '1,0': self.assertEqual(first['references'][key], result['references'][key])
        self.assert_not_published()

    def test_reconcile_removed_tiles_and_new_tiles(self):
        first = hbf.step(self.out)
        raw = json.loads((self.out/'index.json').read_text())
        del raw['tiles']['0,0']
        self.alias('6,0').write_bytes(tile_bytes('6,0'))
        raw['tiles']['6,0'] = hashlib.sha256(self.alias('6,0').read_bytes()).hexdigest()[:12]
        (self.out/'index.json').write_text(json.dumps(raw))
        result = hbf.step(self.out, max_tiles=1, reconcile=True)
        self.assertTrue(result['complete'])
        self.assertNotIn('0,0', result['references'])
        self.assertIn('6,0', result['references'])
        self.assertTrue((self.out/'objects'/f'{first["references"]["0,0"]}.uih').exists())

    def test_reconcile_refuses_renderer_or_atlas_changes(self):
        hbf.step(self.out, max_tiles=2)
        original = (self.out/'index.json').read_bytes()
        for field in ('identity', 'atlasSha256'):
            raw = json.loads(original)
            raw[field] = 'a'*64
            (self.out/'index.json').write_text(json.dumps(raw))
            self.assert_refused('legacy index changed', reconcile=True)
        self.assert_not_published()

    def test_reconcile_does_not_relax_mid_step_snapshot_guard(self):
        hbf.step(self.out, max_tiles=1)
        self.replace_alias('4,0', tile_bytes('4,0', shift=2))
        self.publish_legacy()
        real = hb.ingest_tile
        def changed_during_ingest(out, source, key):
            result = real(out, source, key)
            raw = (self.out/'index.json').read_bytes()
            (self.out/'index.json').write_bytes(raw+b' ')
            return result
        with patch.object(hb, 'ingest_tile', side_effect=changed_during_ingest):
            self.assert_refused('changed during this step', reconcile=True)
        self.assert_not_published()

    def test_reconciled_references_still_require_full_content_verification(self):
        first = hbf.step(self.out)
        # Corrupt an old object in place: a progress reference is NOT a content proof.
        (self.out/'objects'/f'{first["references"]["0,0"]}.uih').write_bytes(b'corrupt')
        self.replace_alias('4,0', tile_bytes('4,0', shift=2))
        raw = json.loads((self.out/'index.json').read_text())
        raw['tiles']['4,0'] = hashlib.sha256(self.alias('4,0').read_bytes()).hexdigest()[:12]
        (self.out/'index.json').write_text(json.dumps(raw))
        result = hbf.step(self.out, reconcile=True)
        with self.assertRaises(hb.BundleError):
            hb.publish_index(self.out, result['references'], result['atlasSha256'], result['rendererIdentity'])
        self.assertFalse((self.out/'current.json').exists())

    def test_tile_not_matching_its_sha12_is_not_advanced(self):
        hbf.step(self.out, max_tiles=1)
        self.replace_alias('1,0', tile_bytes('1,0', shift=3))         # index still lists the old sha12
        self.assert_refused('no longer matches its legacy sha12')
        self.assert_refused('no longer matches its legacy sha12')
        self.assertEqual(sorted(json.loads(self.progress())['objects']), ['0,0'])

    def test_index_changed_during_a_step_keeps_the_progress(self):
        hbf.step(self.out, max_tiles=1)
        original = (self.out / 'index.json').read_bytes()
        real = hb.ingest_tile
        def republish(out, source, key):
            tmp = self.out / 'index.tmp'
            tmp.write_bytes(original + b' ')
            os.replace(tmp, self.out / 'index.json')
            return real(out, source, key)
        with patch.object(hb, 'ingest_tile', side_effect=republish):
            self.assert_refused('changed during this step')

    def test_empty_malformed_index_and_invalid_options(self):
        for kw in ({'max_tiles': 0}, {'max_tiles': True}, {'max_tiles': hb.MAX_INDEX_ENTRIES + 1},
                   {'max_source_bytes': 0}, {'max_source_bytes': hbf.HARD_MAX_SOURCE_BYTES + 1}, {'restart': 'yes'},
                   {'reconcile': 'yes'}, {'restart': True, 'reconcile': True}):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    hbf.step(self.out, **kw)
        doc = json.loads((self.out / 'index.json').read_bytes())
        (self.out / 'index.json').write_text(json.dumps({**doc, 'tiles': {}}), encoding='utf-8')
        self.assert_refused('empty map is not backfilled')
        (self.out / 'index.json').write_text(json.dumps({**doc, 'tileSize': 32}), encoding='utf-8')
        self.assert_refused('tileSize')
        self.assertIsNone(self.progress())
        self.assertFalse((self.out / 'objects').exists())
        self.assert_not_published()


if __name__ == '__main__':
    unittest.main()

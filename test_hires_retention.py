import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import hires_bundle as hb
import hires_backfill as bf
import hires_history as hh
import hires_retention as hr


class RetentionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name).resolve()
        (self.out / hb.OWNED_MARKER).write_text('test')
        for folder in ('indexes', 'objects', 'atlases', 'layouts'):
            (self.out / folder).mkdir()
        self.base = time.time() + 10_000

    def asset(self, folder, suffix, label):
        raw = label.encode()
        sha = hashlib.sha256(raw).hexdigest()
        (self.out / folder / f'{sha}{suffix}').write_bytes(raw)
        return sha

    def index(self, label, *, v2=False):
        tile = self.asset('objects', '.uih', 'tile-' + label)
        atlas = self.asset('atlases', '.png', 'atlas-' + label)
        doc = {'format': hb.INDEX_FORMAT_V2 if v2 else hb.INDEX_FORMAT,
               'tileSize': 64, 'rendererIdentity': hashlib.sha256(label.encode()).hexdigest(),
               'atlasSha256': atlas, 'tiles': {'0,0': tile}}
        layout = None
        if v2:
            layout = self.asset('layouts', '.json', 'layout-' + label)
            doc['context'] = {'world': 'world', 'dimension': 'overworld', 'renderer': label,
                              'caves': False, 'layoutSha256': layout}
        raw = json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()
        sha = hashlib.sha256(raw).hexdigest()
        (self.out / 'indexes' / f'{sha}.json').write_bytes(raw)
        return sha, tile, atlas, layout

    def pointer(self, sha):
        (self.out / 'current.json').write_bytes(json.dumps({'format': hb.POINTER_FORMAT,
                                                            'indexSha256': sha}).encode())

    def prepare(self, *, v2=False):
        a = self.index('a')
        b = self.index('b', v2=v2)
        hh.record_publication(self.out, a[0], now=self.base - 3600)
        hh.record_publication(self.out, b[0], a[0], now=self.base - 100)
        self.pointer(b[0])
        return a, b

    def progress(self, *, valid=True):
        tile = self.asset('objects', '.uih', 'progress-tile')
        atlas = self.asset('atlases', '.png', 'progress-atlas')
        legacy = {'format': 'UIE_HIRES_INDEX1', 'tileSize': 64,
                  'atlas': 'atlas.png', 'atlasSha256': atlas,
                  'identity': hashlib.sha256(b'legacy-renderer').hexdigest(),
                  'renderer': 'test', 'caves': False, 'tiles': {'5,0': tile[:12]}}
        raw = json.dumps(legacy, sort_keys=True, separators=(',', ':')).encode()
        (self.out / 'index.json').write_bytes(raw)
        state = {'format': bf.PROGRESS_FORMAT,
                 'legacyIndexSha256': hashlib.sha256(raw).hexdigest(),
                 'rendererIdentity': legacy['identity'], 'atlasSha256': atlas,
                 'tileCount': 1, 'atlasLinked': True, 'objects': {'5,0': tile}}
        if not valid:
            state['objects'] = {'5,0': 'f' * 64}
        (self.out / bf.PROGRESS).write_bytes(json.dumps(state, sort_keys=True, separators=(',', ':')).encode())
        return tile, atlas

    def test_current_previous_and_v2_layout_are_retained(self):
        a, b = self.prepare(v2=True)
        orphan = self.index('orphan')
        (self.out / 'objects' / 'notes.txt').write_text('unknown')
        result = hr.plan_retention(self.out, now=self.base)
        paths = {item['path'] for item in result['candidates']}
        self.assertNotIn(f'indexes/{a[0]}.json', paths)
        self.assertNotIn(f'layouts/{b[3]}.json', paths)
        self.assertIn(f'indexes/{orphan[0]}.json', paths)
        self.assertIn(f'objects/{orphan[1]}.uih', paths)
        self.assertEqual(result['unknown']['objects'], 1)
        for item in result['candidates']:
            self.assertEqual(set(item['identity']), {'device', 'inode', 'size', 'mtimeNs', 'ctimeNs'})

    def test_actual_pointer_is_authoritative_when_journal_roots_differ(self):
        a, b = self.prepare()
        c = self.index('pointer-only')
        self.pointer(c[0])
        result = hr.plan_retention(self.out, now=self.base + 5000)
        paths = {item['path'] for item in result['candidates']}
        self.assertEqual(result['currentIndexSha256'], c[0])
        self.assertNotIn(f'indexes/{c[0]}.json', paths)
        self.assertNotIn(f'objects/{c[1]}.uih', paths)
        self.assertNotIn(f'indexes/{a[0]}.json', paths)
        self.assertNotIn(f'indexes/{b[0]}.json', paths)

    def test_aba_and_retirement_grace(self):
        a, b = self.prepare()
        c = self.index('c')
        hh.record_publication(self.out, a[0], b[0], now=self.base)
        self.pointer(a[0])
        hh.record_publication(self.out, c[0], a[0], now=self.base + 100)
        self.pointer(c[0])
        plan = hr.plan_retention(self.out, now=self.base + 1800)
        self.assertNotIn(f'indexes/{a[0]}.json', {c['path'] for c in plan['candidates']})
        self.assertNotIn(f'indexes/{b[0]}.json', {c['path'] for c in plan['candidates']})
        plan = hr.plan_retention(self.out, now=self.base + 1901)
        self.assertIn(f'indexes/{b[0]}.json', {c['path'] for c in plan['candidates']})
        self.assertNotIn(f'indexes/{a[0]}.json', {c['path'] for c in plan['candidates']})

    def test_young_index_mtime_protects_it_and_its_closure(self):
        a, _ = self.prepare()
        young = self.index('young')
        # The fresh orphan is outside the journal but still within filesystem grace.
        result = hr.plan_retention(self.out, now=time.time())
        paths = {item['path'] for item in result['candidates']}
        self.assertNotIn(f'indexes/{young[0]}.json', paths)
        self.assertNotIn(f'objects/{young[1]}.uih', paths)

    def test_missing_or_damaged_retained_graph_refuses_whole_plan(self):
        _, b = self.prepare()
        (self.out / 'objects' / f'{b[1]}.uih').unlink()
        with self.assertRaises(FileNotFoundError):
            hr.plan_retention(self.out, now=self.base)
        (self.out / 'objects' / f'{b[1]}.uih').write_bytes(b'restored')
        (self.out / 'indexes' / f'{b[0]}.json').write_bytes(b'{}')
        with self.assertRaisesRegex(hb.BundleError, 'hash mismatch'):
            hr.plan_retention(self.out, now=self.base)

    def test_noncanonical_but_hash_valid_retained_index_refuses(self):
        canonical, _, _, _ = self.index('canonical')
        doc = json.loads((self.out / 'indexes' / f'{canonical}.json').read_bytes())
        raw = json.dumps(doc, indent=2).encode()
        alternate = hashlib.sha256(raw).hexdigest()
        (self.out / 'indexes' / f'{alternate}.json').write_bytes(raw)
        hh.record_publication(self.out, alternate, now=self.base)
        self.pointer(alternate)
        with self.assertRaisesRegex(hb.BundleError, 'not canonical'):
            hr.plan_retention(self.out, now=self.base)

    def test_pointer_journal_and_symlinks_refuse(self):
        _, b = self.prepare()
        journal = self.out / hh.NAME
        original = journal.read_bytes()
        journal.write_bytes(b'{}')
        with self.assertRaises(hb.BundleError):
            hr.plan_retention(self.out, now=self.base)
        journal.write_bytes(original)
        object_path = self.out / 'objects' / f'{b[1]}.uih'
        object_path.unlink()
        try:
            object_path.symlink_to(self.out / 'current.json')
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaisesRegex(hb.BundleError, 'symlink'):
            hr.plan_retention(self.out, now=self.base)

    def test_pointer_change_during_scan_refuses(self):
        self.prepare()
        original = hb._read
        seen = 0

        def moving(path, limit):
            nonlocal seen
            raw, info = original(path, limit)
            if path.name == 'current.json':
                seen += 1
                if seen == 2:
                    return raw + b' ', info
            return raw, info

        with patch.object(hb, '_read', side_effect=moving):
            with self.assertRaisesRegex(hb.BundleError, 'pointer changed'):
                hr.plan_retention(self.out, now=self.base)

    def test_same_bytes_pointer_replacement_during_scan_refuses(self):
        self.prepare()
        original_scan = hr._scan

        def replacing(out, charge):
            pointer = out / 'current.json'
            replacement = out / 'replacement.json'
            replacement.write_bytes(pointer.read_bytes())
            os.replace(replacement, pointer)
            return original_scan(out, charge)

        with patch.object(hr, '_scan', side_effect=replacing):
            with self.assertRaisesRegex(hb.BundleError, 'pointer changed'):
                hr.plan_retention(self.out, now=self.base)

    def test_backfill_progress_conservatively_protects_objects(self):
        self.prepare()
        orphan = self.asset('objects', '.uih', 'orphan')
        (self.out / 'backfill-progress.json').write_bytes(b'possibly incomplete')
        result = hr.plan_retention(self.out, now=self.base)
        self.assertTrue(result['preparationProgressPresent'])
        self.assertFalse(result['preparationProgressValid'])
        self.assertNotIn(f'objects/{orphan}.uih', {c['path'] for c in result['candidates']})

    def test_valid_progress_protects_exact_references_and_exposes_orphans(self):
        self.prepare()
        tile, atlas = self.progress()
        orphan_tile = self.asset('objects', '.uih', 'unreferenced-tile')
        orphan_atlas = self.asset('atlases', '.png', 'unreferenced-atlas')
        orphan_layout = self.asset('layouts', '.json', 'unreferenced-layout')
        result = hr.plan_retention(self.out, now=self.base)
        paths = {item['path'] for item in result['candidates']}
        self.assertTrue(result['preparationProgressValid'])
        self.assertNotIn(f'objects/{tile}.uih', paths)
        self.assertNotIn(f'atlases/{atlas}.png', paths)
        self.assertIn(f'objects/{orphan_tile}.uih', paths)
        self.assertIn(f'atlases/{orphan_atlas}.png', paths)
        self.assertIn(f'layouts/{orphan_layout}.json', paths)

    def test_invalid_progress_reference_keeps_blanket_protection(self):
        self.prepare()
        self.progress(valid=False)
        orphan = self.asset('objects', '.uih', 'unreferenced')
        result = hr.plan_retention(self.out, now=self.base)
        self.assertFalse(result['preparationProgressValid'])
        self.assertNotIn(f'objects/{orphan}.uih', {item['path'] for item in result['candidates']})

    def test_progress_replacement_during_scan_refuses(self):
        self.prepare()
        self.progress()
        original_scan = hr._scan

        def replacing(out, charge):
            path = out / bf.PROGRESS
            replacement = out / 'replacement-progress.json'
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
            return original_scan(out, charge)

        with patch.object(hr, '_scan', side_effect=replacing):
            with self.assertRaisesRegex(hb.BundleError, 'progress changed'):
                hr.plan_retention(self.out, now=self.base)

    def test_progress_budget_prevents_read(self):
        self.prepare()
        self.progress()
        initial = (self.out / 'current.json').stat().st_size + (self.out / hh.NAME).stat().st_size
        progress_size = (self.out / bf.PROGRESS).stat().st_size
        original_read = hb._read

        def no_progress_read(path, limit):
            if path.name == bf.PROGRESS:
                raise AssertionError('progress read exceeded remaining metadata budget')
            return original_read(path, limit)

        with patch.object(hr, 'MAX_METADATA_BYTES', initial + progress_size - 1):
            with patch.object(hb, '_read', side_effect=no_progress_read):
                with self.assertRaisesRegex(hb.BundleError, 'metadata byte capacity'):
                    hr.plan_retention(self.out, now=self.base)

    def test_legacy_binding_budget_prevents_read(self):
        self.prepare()
        self.progress()
        initial = sum((self.out / name).stat().st_size
                      for name in ('current.json', hh.NAME, bf.PROGRESS))
        legacy_size = (self.out / 'index.json').stat().st_size
        original_read = hb._read

        def no_legacy_read(path, limit):
            if path.name == 'index.json':
                raise AssertionError('legacy index read exceeded remaining metadata budget')
            return original_read(path, limit)

        with patch.object(hr, 'MAX_METADATA_BYTES', initial + legacy_size - 1):
            with patch.object(hb, '_read', side_effect=no_legacy_read):
                with self.assertRaisesRegex(hb.BundleError, 'metadata byte capacity'):
                    hr.plan_retention(self.out, now=self.base)

    def test_scan_and_candidate_output_are_bounded(self):
        self.prepare()
        with patch.object(hr, 'MAX_SCANNED', 1):
            with self.assertRaisesRegex(hb.BundleError, 'entry capacity'):
                hr.plan_retention(self.out, now=self.base)
        with patch.object(hr, 'MAX_CANDIDATES', 1):
            result = hr.plan_retention(self.out, now=self.base)
        self.assertLessEqual(len(result['candidates']), 1)
        self.assertEqual(result['candidateCount'], len(result['candidates']) + result['candidatesOmitted'])

    def test_exhausted_metadata_budget_prevents_index_read(self):
        index, _, _, _ = self.index('budget')
        hh.record_publication(self.out, index, now=self.base)
        self.pointer(index)
        initial = (self.out / 'current.json').stat().st_size + (self.out / hh.NAME).stat().st_size
        for folder in ('indexes', 'objects', 'atlases', 'layouts'):
            with os.scandir(self.out / folder) as iterator:
                initial += sum(128 + len(entry.name.encode('utf-8', 'surrogatepass')) for entry in iterator)
        index_size = (self.out / 'indexes' / f'{index}.json').stat().st_size
        original_read = hb._read

        def no_index_read(path, limit):
            if path.parent.name == 'indexes':
                raise AssertionError('index read exceeded remaining metadata budget')
            return original_read(path, limit)

        with patch.object(hr, 'MAX_METADATA_BYTES', initial + index_size - 1):
            with patch.object(hb, '_read', side_effect=no_index_read):
                with self.assertRaisesRegex(hb.BundleError, 'metadata byte capacity'):
                    hr.plan_retention(self.out, now=self.base)


if __name__ == '__main__':
    unittest.main()

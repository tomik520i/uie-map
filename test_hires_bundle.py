"""Private immutable hires prototype tests; never touches a live store."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hires_bundle as hb
import world_hires as wh
from test_world_hires import quad


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name).resolve() / 'bundle'
        self.out.mkdir()
        (self.out / hb.OWNED_MARKER).write_text('private test')
        self.alias = self.out / 'legacy.uih'
        self.alias.write_bytes(wh.encode_tile(0, 0, [quad(1, 64, 1)]))
        self.atlas = self.out / 'atlas.png'
        self.atlas.write_bytes(hb.PNG_SIGNATURE + b'\x00' * 32)

    def ready(self):
        tile = hb.ingest_tile(self.out, self.alias, '0,0')
        atlas = hb.ingest_atlas(self.out, self.atlas)
        return tile, atlas

    def pointer(self):
        return (self.out / 'current.json').read_bytes()

    def test_ingest_uses_same_inode_and_alias_replacement_keeps_old_object(self):
        original = self.alias.read_bytes()
        tile, atlas = self.ready()
        obj = self.out / 'objects' / f'{tile}.uih'
        self.assertEqual(os.stat(self.alias).st_ino, os.stat(obj).st_ino)
        self.assertEqual(os.stat(self.atlas).st_ino, os.stat(self.out/'atlases'/f'{atlas}.png').st_ino)
        replacement = self.out / 'new.uih'
        replacement.write_bytes(wh.encode_tile(0, 0, [quad(2, 65, 2)]))
        os.replace(replacement, self.alias)
        self.assertEqual(obj.read_bytes(), original)
        self.assertNotEqual(self.alias.read_bytes(), original)

    def test_publish_valid_refs_and_keep_pointer_on_bad_ref(self):
        tile, atlas = self.ready()
        result = hb.publish_index(self.out, {'0,0': tile}, atlas, 'a' * 64)
        pointer = self.pointer()
        self.assertEqual(json.loads(pointer)['indexSha256'], result['indexSha256'])
        for refs in ({'0,0': 'f' * 64}, {'1,0': tile}):
            with self.assertRaises((hb.BundleError, FileNotFoundError)):
                hb.publish_index(self.out, refs, atlas, 'a' * 64)
            self.assertEqual(self.pointer(), pointer)

    def test_mutated_source_and_unsupported_hardlink_leave_pointer(self):
        tile, atlas = self.ready()
        hb.publish_index(self.out, {'0,0': tile}, atlas, 'a' * 64)
        pointer = self.pointer()
        changed = self.out/'changed.uih'
        changed.write_bytes(wh.encode_tile(1 * wh.TILE, 0, [quad(65, 64, 1)]))
        real_link = os.link
        def mutate(source, target, **kw):
            replacement = self.out/'replacement.uih'
            replacement.write_bytes(wh.encode_tile(1 * wh.TILE, 0, [quad(66, 64, 1)]))
            os.replace(replacement, source)
            return real_link(source, target, **kw)
        with patch.object(hb.os, 'link', side_effect=mutate):
            with self.assertRaises(hb.BundleError):
                hb.ingest_tile(self.out, changed, '1,0')
        self.assertEqual(self.pointer(), pointer)
        with patch.object(hb.os, 'link', side_effect=OSError('no hardlinks')):
            with self.assertRaisesRegex(hb.BundleError, 'hardlink unavailable'):
                hb.ingest_tile(self.out, changed, '1,0')
        self.assertEqual(self.pointer(), pointer)

    def race_source(self):
        """Source A for tile 1,0 and a different valid UIH B that replaces it right before os.link."""
        source = self.out/'race.uih'
        original = wh.encode_tile(1 * wh.TILE, 0, [quad(65, 64, 1)])
        source.write_bytes(original)
        swapped = wh.encode_tile(1 * wh.TILE, 0, [quad(66, 64, 1)])
        real_link = os.link
        def swap_then_link(src, dst, **kw):
            replacement = self.out/'swap.uih'
            replacement.write_bytes(swapped)
            os.replace(replacement, src)
            return real_link(src, dst, **kw)
        return source, original, swapped, swap_then_link

    def test_race_removes_created_link_and_retry_succeeds(self):
        source, original, swapped, swap_then_link = self.race_source()
        target = self.out/'objects'/f'{hashlib.sha256(original).hexdigest()}.uih'
        synced = []
        real_sync = hb._fsync_dir
        with patch.object(hb.os, 'link', side_effect=swap_then_link), \
             patch.object(hb, '_fsync_dir', side_effect=lambda p: (synced.append(p), real_sync(p))):
            with self.assertRaisesRegex(hb.BundleError, 'differs from its content address'):
                hb.ingest_tile(self.out, source, '1,0')
        self.assertFalse(target.exists(), 'no poisoned content-addressed name is left')
        self.assertIn(self.out/'objects', synced, 'directory synced after the cleanup')
        self.assertEqual(source.read_bytes(), swapped, 'the source name itself is never touched')
        stable = self.out/'stable.uih'
        stable.write_bytes(original)
        os.replace(stable, source)
        self.assertEqual(hb.ingest_tile(self.out, source, '1,0'), hashlib.sha256(original).hexdigest())
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(os.stat(target).st_ino, os.stat(source).st_ino)

    def test_failed_cleanup_keeps_the_original_error(self):
        source, original, _swapped, swap_then_link = self.race_source()
        with patch.object(hb.os, 'link', side_effect=swap_then_link), \
             patch.object(hb.os, 'unlink', side_effect=OSError('unlink refused')):
            with self.assertRaisesRegex(hb.BundleError, 'differs from its content address'):
                hb.ingest_tile(self.out, source, '1,0')

    def test_cleanup_skips_a_name_that_no_longer_is_the_created_inode(self):
        source, original, _swapped, swap_then_link = self.race_source()
        target = self.out/'objects'/f'{hashlib.sha256(original).hexdigest()}.uih'
        foreign = b'not the linked inode'
        real_read = hb._read
        def replace_target_then_read(path, limit):
            if Path(path) == target:
                other = self.out/'objects'/'other.tmp'
                other.write_bytes(foreign)
                os.replace(other, target)
            return real_read(path, limit)
        with patch.object(hb.os, 'link', side_effect=swap_then_link), \
             patch.object(hb, '_read', side_effect=replace_target_then_read):
            with self.assertRaises(hb.BundleError):
                hb.ingest_tile(self.out, source, '1,0')
        self.assertEqual(target.read_bytes(), foreign, 'a different inode under the name is not removed')

    def test_preexisting_invalid_object_is_preserved_and_fails_closed(self):
        source = self.out/'valid.uih'
        original = wh.encode_tile(1 * wh.TILE, 0, [quad(65, 64, 1)])
        source.write_bytes(original)
        objects = self.out/'objects'
        objects.mkdir()
        target = objects/f'{hashlib.sha256(original).hexdigest()}.uih'
        junk = wh.encode_tile(1 * wh.TILE, 0, [quad(66, 64, 1)])
        target.write_bytes(junk)
        for _attempt in range(2):
            with self.assertRaisesRegex(hb.BundleError, 'differs from its content address'):
                hb.ingest_tile(self.out, source, '1,0')
            self.assertEqual(target.read_bytes(), junk, 'pre-existing objects are never removed')
        atlas = hb.ingest_atlas(self.out, self.atlas)
        with self.assertRaisesRegex(hb.BundleError, 'object hash mismatch'):
            hb.publish_index(self.out, {'1,0': hashlib.sha256(original).hexdigest()}, atlas, 'a' * 64)
        self.assertFalse((self.out/'current.json').exists())

    def test_bad_header_bad_png_and_foreign_store_refused(self):
        with self.assertRaises(hb.BundleError):
            hb.ingest_tile(self.out, self.alias, '1,0')
        self.atlas.write_bytes(b'not a png')
        with self.assertRaises(hb.BundleError):
            hb.ingest_atlas(self.out, self.atlas)
        (self.out / hb.OWNED_MARKER).unlink()
        with self.assertRaises((hb.BundleError, FileNotFoundError)):
            hb.ingest_tile(self.out, self.alias, '0,0')

    def test_index_sync_failure_does_not_publish_pointer(self):
        tile, atlas = self.ready()
        original = hb._fsync_dir
        def refuse_indexes(path):
            if path.name == 'indexes':
                raise OSError('directory sync failed')
            return original(path)
        with patch.object(hb, '_fsync_dir', side_effect=refuse_indexes):
            with self.assertRaisesRegex(OSError, 'directory sync failed'):
                hb.publish_index(self.out, {'0,0': tile}, atlas, 'a' * 64)
        self.assertFalse((self.out/'current.json').exists())

    def test_existing_index_is_synced_again_before_pointer(self):
        tile, atlas = self.ready()
        hb.publish_index(self.out, {'0,0': tile}, atlas, 'a' * 64)
        synced = []
        with patch.object(hb, '_fsync_dir', side_effect=lambda path: synced.append(path)):
            hb.publish_index(self.out, {'0,0': tile}, atlas, 'a' * 64)
        self.assertEqual(synced, [self.out/'indexes', self.out])

    def test_symlinked_object_parent_refuses_before_pointer(self):
        tile, atlas = self.ready()
        hb.publish_index(self.out, {'0,0': tile}, atlas, 'a' * 64)
        pointer = self.pointer()
        original = self.out/'objects'
        real = self.out/'objects-real'
        os.rename(original, real)
        try:
            os.symlink(real, original, target_is_directory=True)
        except (OSError, NotImplementedError):
            os.rename(real, original)
            self.skipTest('directory symlinks unavailable')
        with self.assertRaisesRegex(hb.BundleError, 'real directory'):
            hb.publish_index(self.out, {'0,0': tile}, atlas, 'a' * 64)
        self.assertEqual(self.pointer(), pointer)


if __name__ == '__main__':
    unittest.main()

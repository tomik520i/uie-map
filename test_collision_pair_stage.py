import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import collision_bundle as cb
import collision_pair_stage as stage
import test_collision_tile as fixtures
from test_world_hires import quad


class PairStageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = self.root / 'collision'; self.store.mkdir()
        self.hires = self.root / 'hires'; (self.hires / 'objects').mkdir(parents=True)
        self.volume = fixtures.CollisionTileTests().fixture()
        self.mesh = {'chunks': {(-4, -4): quad(-63, 64, -63)}}
        self.geometry = stage.wh.encode_tile(-64, -64, [self.mesh['chunks'][(-4, -4)]])
        self.sha = hashlib.sha256(self.geometry).hexdigest()
        self.source = self.hires / 'objects' / (self.sha + '.uih'); self.source.write_bytes(self.geometry)
        self.visible = dict(tiles={'-1,-1': self.sha}, stale=set(), atlasSha256='a'*64,
                            identity='b'*64, indexSha256='c'*64)
        self.read = patch.object(stage.probe, 'read_visible', return_value=self.visible).start()
        self.addCleanup(patch.stopall)

    def call(self, **kw):
        return stage.stage_pair(self.store, self.hires, self.volume, self.mesh, min_free_bytes=0, **kw)

    def test_same_pair_bytes_as_existing_exporter_with_shared_geometry_and_no_pointer(self):
        old = self.root/'old'; old.mkdir()
        expected = cb.publish_pair(old, self.volume, self.mesh, 'a'*64)
        actual = self.call()
        self.assertEqual(actual['pairSha256'], expected['pairSha256'])
        self.assertTrue(actual['geometryShared'])
        target = self.store/'objects'/self.source.name
        self.assertTrue(target.samefile(self.source))
        self.assertEqual(cb._verify_pair(self.store, '-1,-1', actual['pairSha256']), '-1,-1')
        self.assertFalse((self.store/'current.json').exists())
        self.assertEqual(self.call()['pairSha256'], actual['pairSha256'])

    def test_mismatched_mesh_budget_and_corrupt_source_refuse_without_pair(self):
        self.visible['tiles']['-1,-1'] = 'd'*64
        with self.assertRaisesRegex(ValueError, 'mesh differs'): self.call()
        self.visible['tiles']['-1,-1'] = self.sha
        with self.assertRaisesRegex(ValueError, 'budget'): self.call(max_new_bytes=1)
        self.source.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'differs'): self.call()
        self.assertFalse(list(self.store.glob('pairs/*.json')))

    def test_pointer_movement_refuses_pair_but_never_changes_public_pointer(self):
        changed = dict(self.visible, tiles={'-1,-1': 'd'*64})
        self.read.side_effect = [self.visible, changed]
        with self.assertRaisesRegex(ValueError, 'changed'): self.call()
        self.assertFalse(list(self.store.glob('pairs/*.json')))
        self.assertFalse((self.store/'current.json').exists())

    def test_selected_dimension_reader_is_used_before_and_after_staging(self):
        self.read.side_effect=AssertionError('Must not fall back to main dimension')
        calls=[]
        def selected(path):
            calls.append(path)
            return self.visible
        self.call(read_index=selected)
        self.assertEqual(calls,[self.hires,self.hires]);self.read.assert_not_called()

    def test_corrupt_existing_object_never_overwritten(self):
        pair = self.call()
        block = self.store/'objects'/(pair['blocksSha256']+'.uib'); block.write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'differs'): self.call()
        self.assertEqual(block.read_bytes(), b'bad')

    def test_symlink_source_refused_and_source_not_modified(self):
        original = self.hires/'original'; self.source.rename(original)
        self.source.symlink_to(original)
        with self.assertRaisesRegex(ValueError, 'non-symlink'): self.call()
        self.assertEqual(original.read_bytes(), self.geometry)

    def test_geometry_change_during_link_refuses_pair(self):
        link = stage.os.link
        def mutate(source, target, **kw):
            link(source, target, **kw)
            Path(target).write_bytes(b'changed')
        with patch.object(stage.os, 'link', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'changed'): self.call()
        self.assertFalse(list(self.store.glob('pairs/*.json')))

    def test_symlink_existing_blocks_refused(self):
        pair = self.call()
        target = self.store/'objects'/(pair['blocksSha256']+'.uib')
        outside = self.root/'blocks'; target.rename(outside); target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'non-symlink'): self.call()


if __name__ == '__main__': unittest.main()

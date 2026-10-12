"""Store monotonicity of hires_bundle.publish_index (V1 < V2 < V3, fixed world/dimension). Stdlib only.
    python -m unittest test_hires_publish_monotonic -v      (from work/uie-map)
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import hires_bundle as hb
import hires_snapshot as hs
import world_hires as wh
from test_world_hires import quad

IDENTITY = hashlib.sha256(b'renderer').hexdigest()
KEYS = ('0,0', '1,0')


class MonotonicTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name).resolve() / 'hires'
        (self.out / 'tiles').mkdir(parents=True)
        (self.out / hb.OWNED_MARKER).write_text('private test')
        atlas = hb.PNG_SIGNATURE + b'\x00' * 40
        (self.out / 'atlas.png').write_bytes(atlas)
        self.refs = {}
        for key in KEYS:
            tx = int(key.split(',')[0])
            alias = self.out / 'tiles' / f"{key.replace(',', '_')}.uih"
            alias.write_bytes(wh.encode_tile(tx * wh.TILE, 0, [quad(tx * wh.TILE + 1, 64, 1)]))
            self.refs[key] = hb.ingest_tile(self.out, alias, key)
        self.atlas = hb.ingest_atlas(self.out, self.out / 'atlas.png')
        layout = self.out / 'atlas-layout.json'
        layout.write_text(json.dumps({'format': 'UIE_ATLAS_LAYOUT1', 'registered': {}}))
        self.layout = hb.ingest_layout(self.out, layout)
        # A valid legacy output too, so the private snapshot entrypoint can run against the same store.
        meta = {'renderer': 'uie-offline-mesher/1+hires', 'identity': IDENTITY, 'caves': False,
                'atlas': 'atlas.png', 'atlasSha256': self.atlas}
        wh._write_index(self.out, meta, {(0, 0): {'tiles': {k: s[:12] for k, s in self.refs.items()}, 'chunks': 2}})

    def context(self, **changes):
        return {'world': 'uie', 'dimension': 'overworld', 'renderer': 'r', 'caves': False,
                'layoutSha256': self.layout, **changes}

    def source(self, **changes):
        return {'legacyIndexSha256': 'a' * 64, 'producerFinishedAt': 1, 'planComplete': True, 'plannedJobs': 1,
                'notScheduled': 0, 'failedJobs': 0, 'failedTiles': 0, 'staleTiles': [], **changes}

    def publish(self, version, *, context=None, source=None, **kw):
        context = (context or self.context()) if version >= 2 else None
        source = (source or self.source()) if version == 3 else None
        return hb.publish_index(self.out, self.refs, self.atlas, IDENTITY, context=context, source=source, **kw)

    def state(self):
        pointer = self.out / 'current.json'
        indexes = self.out / 'indexes'
        return (pointer.read_bytes() if pointer.exists() else None,
                sorted(os.listdir(indexes)) if indexes.exists() else [])

    def current_format(self):
        sha = json.loads((self.out / 'current.json').read_bytes())['indexSha256']
        return json.loads((self.out / 'indexes' / f'{sha}.json').read_bytes())['format']

    def assert_refused(self, call, pattern):
        before = self.state()
        with self.assertRaisesRegex(hb.BundleError, pattern):
            call()
        self.assertEqual(self.state(), before, 'old pointer kept, no new index written')

    def write_index(self, raw):
        (self.out / 'indexes').mkdir(exist_ok=True)
        sha = hashlib.sha256(raw).hexdigest()
        (self.out / 'indexes' / f'{sha}.json').write_bytes(raw)
        return sha

    def point(self, sha):
        (self.out / 'current.json').write_bytes(json.dumps({'format': hb.POINTER_FORMAT, 'indexSha256': sha},
                                                           sort_keys=True, separators=(',', ':')).encode())

    def test_empty_store_accepts_a_first_v1_snapshot(self):
        result = hs.snapshot(self.out)
        self.assertEqual(self.current_format(), hb.INDEX_FORMAT)
        self.assertEqual(json.loads((self.out / 'current.json').read_bytes())['indexSha256'], result['indexSha256'])
        self.publish(1)                                  # V1 -> V1 stays allowed

    def test_upgrades_and_same_level_republication_are_allowed(self):
        self.publish(1)
        self.publish(2)
        self.assertEqual(self.current_format(), hb.INDEX_FORMAT_V2)
        self.publish(3)
        self.assertEqual(self.current_format(), hb.INDEX_FORMAT_V3)
        self.publish(3, source=self.source(producerFinishedAt=2))
        self.publish(3, context=self.context(renderer='other', caves=True))   # renderer/caves: the session's rule
        self.assertEqual(self.current_format(), hb.INDEX_FORMAT_V3)

    def test_v1_to_v3_directly_is_an_upgrade(self):
        self.publish(1)
        self.publish(3)
        self.assertEqual(self.current_format(), hb.INDEX_FORMAT_V3)

    def test_downgrades_are_refused_by_publish_index(self):
        self.publish(3)
        self.assert_refused(lambda: self.publish(2), 'cannot downgrade')
        self.assert_refused(lambda: self.publish(1), 'cannot downgrade')
        self.assertEqual(self.current_format(), hb.INDEX_FORMAT_V3)

    def test_v2_to_v1_is_refused(self):
        self.publish(2)
        self.assert_refused(lambda: self.publish(1), 'cannot downgrade')

    def test_snapshot_entrypoint_cannot_downgrade_a_v3_store(self):
        self.publish(3)
        self.assert_refused(lambda: hs.snapshot(self.out), 'cannot downgrade')
        self.assertEqual(self.current_format(), hb.INDEX_FORMAT_V3)

    def test_world_or_dimension_change_is_refused(self):
        for version in (2, 3):
            with self.subTest(version=version):
                self.publish(version)
                self.assert_refused(lambda: self.publish(3, context=self.context(world='another')), 'world/dimension')
                self.assert_refused(lambda: self.publish(3, context=self.context(dimension='nether')), 'world/dimension')

    def test_history_reuses_the_validated_pointer(self):
        first = self.publish(2, record_history=True)
        second = self.publish(3, record_history=True)
        journal = json.loads((self.out / 'publication-history.json').read_bytes())
        self.assertEqual((journal['currentIndexSha256'], journal['previousIndexSha256']),
                         (second['indexSha256'], first['indexSha256']))

    def test_invalid_current_pointer_or_index_fails_closed(self):
        self.publish(2)
        good_pointer = (self.out / 'current.json').read_bytes()
        good_sha = json.loads(good_pointer)['indexSha256']
        good_doc = json.loads((self.out / 'indexes' / f'{good_sha}.json').read_bytes())
        canonical = lambda doc: json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()
        broken = {
            'pointer not json': lambda: (self.out / 'current.json').write_bytes(b'not json'),
            'pointer extra key': lambda: (self.out / 'current.json').write_bytes(
                canonical({'format': hb.POINTER_FORMAT, 'indexSha256': good_sha, 'x': 1})),
            'pointer format': lambda: (self.out / 'current.json').write_bytes(
                canonical({'format': 'UIE_HIRES_POINTER2', 'indexSha256': good_sha})),
            'pointer uppercase': lambda: self.point(good_sha.upper()),
            'missing index': lambda: self.point('f' * 64),
            'index bytes changed': lambda: (self.out / 'indexes' / f'{good_sha}.json').write_bytes(
                canonical({**good_doc, 'rendererIdentity': 'b' * 64})),
            'non-canonical index': lambda: self.point(self.write_index(json.dumps(good_doc).encode())),
            'unknown format': lambda: self.point(self.write_index(
                canonical({**good_doc, 'format': 'UIE_HIRES_IMMUTABLE_INDEX4'}))),
            'v2 without context': lambda: self.point(self.write_index(
                canonical({k: v for k, v in good_doc.items() if k != 'context'}))),
            'invalid context': lambda: self.point(self.write_index(
                canonical({**good_doc, 'context': {**good_doc['context'], 'world': ' bad'}}))),
            'invalid v3 source': lambda: self.point(self.write_index(canonical(
                {**good_doc, 'format': hb.INDEX_FORMAT_V3, 'source': self.source(staleTiles=['9,9'])}))),
            'bad tile reference': lambda: self.point(self.write_index(
                canonical({**good_doc, 'tiles': {'0,0': 'X' * 64}}))),
        }
        for label, corrupt in broken.items():
            with self.subTest(label):
                (self.out / 'current.json').write_bytes(good_pointer)
                (self.out / 'indexes' / f'{good_sha}.json').write_bytes(canonical(good_doc))
                corrupt()
                self.assert_refused(lambda: self.publish(3), '')

    def test_invalid_source_and_context_arguments_raise_bundle_error(self):
        with self.assertRaises(hb.BundleError):
            self.publish(3, source=self.source(staleTiles=['9,9']))
        with self.assertRaises(hb.BundleError):
            self.publish(2, context=self.context(world=''))
        self.assertFalse((self.out / 'current.json').exists())


if __name__ == '__main__':
    unittest.main()

"""Offline tests of collision_region_probe with injected boundaries.

Real: collision_export.copy_regions_bounded (on small fake region files), collision_tile.encode_raw/compressed,
world_hires job/tile/strip geometry, read_visible on immutable V3 stores written to disk, Toolkit identity/provenance
with stand-in modules. Fake: chunk decoding, Volume, mesh, UIH encoding, content fingerprints and registry. These
tests show the probe's orchestration and refusals, not real render output.
    python3 -m unittest test_collision_region_probe -v
"""
from array import array
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import collision_region_probe as crp
import collision_tile as ct
import world_hires as wh

ATLAS, IDENTITY = 'a' * 64, 'b' * 64
GOOD = 'c' * 64
REGISTRY = {0: 'minecraft:air', 1: 'minecraft:stone'}
REGION_FILE_BYTES = 8192
CONTEXT = {'world': 'uie', 'dimension': '0', 'renderer': 'uie-offline-mesher/1+hires', 'caves': False,
           'layoutSha256': 'd' * 64}


class FakeVolume:
    registry = REGISTRY

    def __init__(self, core, present):
        self.x0, self.z0, self.present = core.x, core.z, frozenset(present)
        self.ids = array('H', [1] * 64 + [0] * 192) * 4096     # stone up to y 63, air above
        self.metas = bytearray(4096 * 256)

    def column_index(self, x, z):
        dx, dz = x - self.x0, z - self.z0
        return dz * 64 + dx if 0 <= dx < 64 and 0 <= dz < 64 and (x >> 4, z >> 4) in self.present else -1


def geometry_of(x, z):
    return f'geometry {x} {z}'.encode()


def visible_sha(tx, tz):
    return hashlib.sha256(geometry_of(tx * 64, tz * 64)).hexdigest()


def all_tiles():
    return {f'{tx},{tz}': visible_sha(tx, tz) for tx in range(8) for tz in range(8)}


def index(tiles=None, stale=(), atlas=ATLAS, identity=IDENTITY, sha='9' * 64):
    return {'indexSha256': sha, 'atlasSha256': atlas, 'identity': identity,
            'tiles': dict(all_tiles() if tiles is None else tiles), 'stale': set(stale), 'planComplete': not stale}


def write_store(root, tiles, *, fmt=crp.INDEX_FORMAT_V3, context=CONTEXT, stale=(), extra=None, canonical=True,
                pointer=None):
    """A published immutable V3 store as hires_bundle writes it: canonical index named by its SHA-256 + pointer."""
    (root / 'indexes').mkdir(parents=True, exist_ok=True)
    doc = {'format': fmt, 'tileSize': 64, 'rendererIdentity': IDENTITY, 'atlasSha256': ATLAS, 'tiles': tiles,
           'context': context,
           'source': {'legacyIndexSha256': 'e' * 64, 'producerFinishedAt': 1, 'planComplete': not stale,
                      'plannedJobs': 1, 'notScheduled': 0, 'failedJobs': 0, 'failedTiles': 0,
                      'staleTiles': list(stale)}}
    doc.update(extra or {})
    raw = (json.dumps(doc, sort_keys=True, separators=(',', ':')) if canonical else json.dumps(doc, indent=1)).encode()
    sha = hashlib.sha256(raw).hexdigest()
    (root / 'indexes' / f'{sha}.json').write_bytes(raw)
    (root / 'current.json').write_text(json.dumps(pointer or {'format': 'UIE_HIRES_POINTER1', 'indexSha256': sha}))
    return sha


class FakeTools:
    identity, atlas = IDENTITY, ATLAS

    def __init__(self, *, missing=(), bad=(), fingerprints=None, registries=None, indexes=None, read_index=None):
        self.missing, self.bad = set(missing), set(bad)
        self.fingerprints = list(fingerprints or [GOOD] * 3)
        self.registries = list(registries or [REGISTRY, REGISTRY])
        self.indexes = list(indexes) if indexes else None
        if read_index is not None:
            self.read_index = read_index
        self.copy_calls, self.strips, self.copies_seen = 0, [], []
        self.encode_blocks = ct.encode_raw
        self.registry_during = None

    def copy_regions(self, *args, **kw):
        self.copy_calls += 1
        return crp.ce.copy_regions_bounded(*args, **kw)

    def fingerprint(self, region_dir, area):
        return self.fingerprints.pop(0)

    def load_registry(self, world):
        return dict(self.registries.pop(0))

    def read_index(self, hires_store):
        return self.indexes.pop(0) if self.indexes else index()

    def read_chunks(self, copies, strip, failed):
        self.strips.append((strip.z, strip.depth))
        self.copies_seen.append(dict(copies))
        out = {}
        for cz in range(strip.z >> 4, (strip.z + strip.depth) >> 4):
            for cx in range(strip.x >> 4, (strip.x + strip.width) >> 4):
                if (cx, cz) in self.bad:
                    failed.add((cx, cz))
                elif (cx, cz) not in self.missing:
                    out[(cx, cz)] = object()
        return out

    def build_volume(self, t_area, t_core, window):
        self.registry_during = dict(wh._W.get('registry', {}))
        core = {(cx, cz) for cx in range(t_core.x >> 4, (t_core.x >> 4) + 4) for cz in range(t_core.z >> 4, (t_core.z >> 4) + 4)}
        return FakeVolume(t_core, core & set(window))

    def mesh(self, volume):
        return {'chunks': {(0, 0): {'origin': (volume.x0, volume.z0)}}}

    def encode_geometry(self, x, z, docs):
        return geometry_of(x, z)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.world, self.scratch_root = self.root / 'world', self.root / 'scratch'
        (self.world / 'region').mkdir(parents=True)
        self.scratch_root.mkdir()
        for rx in (-1, 0, 1):              # job 0,0 with its 16 block halo touches the 3 x 3 region files
            for rz in (-1, 0, 1):
                (self.world / 'region' / f'r.{rx}.{rz}.mca').write_bytes(bytes([rx & 255, rz & 255]) * (REGION_FILE_BYTES // 2))

    def probe(self, tools, tiles=((0, 0), (1, 0), (0, 1)), hires_store=None, **kw):
        kw.setdefault('disk_free', lambda p: 10 ** 15)
        return crp.probe_region(0, 0, world=self.world, hires_store=hires_store or self.root / 'hires', tools=tools,
                                scratch_root=self.scratch_root, tiles=list(tiles), **kw)

    def assert_clean(self, report):
        self.assertEqual(list(self.scratch_root.iterdir()), [], 'own scratch removed')
        self.assertIs(report.get('scratchCleanup', True), True)

    def test_region_copied_once_strips_decoded_once_and_blocks_are_encode_raw_bytes(self):
        tools = FakeTools()
        report = self.probe(tools)
        self.assertTrue(report['ok'], report['fatal'])
        self.assertEqual(tools.copy_calls, 1)
        self.assertEqual(report['copy'], {'calls': 1, 'files': 9, 'bytes': 9 * REGION_FILE_BYTES})
        self.assertEqual(report['decode']['strips'], 2, 'one 96 block strip per row of selected tiles')
        self.assertEqual(sorted(tools.strips), [(-16, 96), (48, 96)])
        self.assertTrue(all(c == tools.copies_seen[0] for c in tools.copies_seen), 'every strip reads the same copies')
        self.assertTrue(all(Path(p).parent.parent == self.scratch_root for p in tools.copies_seen[0].values()),
                        'copies live in the probe scratch directory')
        for key in ('0,0', '1,0', '0,1'):
            entry = report['tiles'][key]
            tx, tz = map(int, key.split(','))
            _, t_core = wh.tile_area(tx, tz)
            raw = ct.encode_raw(FakeVolume(t_core, {(cx, cz) for cx in range(tx * 4, tx * 4 + 4)
                                                    for cz in range(tz * 4, tz * 4 + 4)}), tx * 64, tz * 64)
            self.assertEqual(entry['result'], 'match')
            self.assertEqual(entry['blockSha256'], ct.identity(raw))
            self.assertEqual(entry['blockRawBytes'], len(raw))
            self.assertEqual(entry['blockGzipBytes'], len(ct.compressed(raw)))
            self.assertEqual(entry['geometrySha256'], visible_sha(tx, tz))
            self.assertEqual(entry['visibleSha12'], visible_sha(tx, tz)[:12])
            self.assertEqual(entry['presentMask'], 0xFFFF)
        self.assertEqual(tools.registry_during, REGISTRY, 'renderer used the level.dat registry snapshot')
        self.assertNotIn('registry', wh._W, 'module registry state restored')
        self.assertEqual(report['source']['before'], report['source']['copy'])
        self.assertEqual(report['visible']['indexSha256'], '9' * 64)
        self.assert_clean(report)

    def test_missing_chunks_stay_unknown_never_air(self):
        tiles = {k: v for k, v in all_tiles().items() if k != '2,0'}
        missing = {(cx, cz) for cx in range(8, 12) for cz in range(4)} | {(12, 0), (13, 0)}
        tools = FakeTools(missing=missing, indexes=[index(tiles), index(tiles)])
        report = self.probe(tools, tiles=[(2, 0), (3, 0)])
        self.assertTrue(report['ok'], report)
        absent = report['tiles']['2,0']
        self.assertEqual((absent['result'], absent['blockSha256'], absent['coreChunks']), ('unknown', None, 0))
        partial = report['tiles']['3,0']
        self.assertEqual(partial['result'], 'match')
        self.assertEqual(partial['presentMask'], 0xFFFF & ~0b11, 'missing chunks are absent in UIB1, not air')
        self.assertEqual(partial['coreChunks'], 14)

    def test_unreadable_halo_chunk_fails_the_tile_and_the_report(self):
        tools = FakeTools(bad={(-1, 0)})                  # halo of tile 0,0, outside every tile core
        report = self.probe(tools, tiles=[(0, 0), (1, 0)])
        self.assertFalse(report['ok'])
        self.assertEqual(report['tiles']['0,0']['result'], 'error')
        self.assertIn('unreadable', report['tiles']['0,0']['reason'])
        self.assertIsNone(report['tiles']['0,0']['blockSha256'], 'never encoded as air')
        self.assertEqual(report['tiles']['1,0']['result'], 'match')
        self.assert_clean(report)

    def test_changed_copy_input_or_registry_refuses_the_probe(self):
        cases = {
            'copy differs': dict(fingerprints=[GOOD, 'd' * 64, GOOD]),
            'input changed after': dict(fingerprints=[GOOD, GOOD, 'e' * 64]),
            'registry changed': dict(registries=[REGISTRY, {**REGISTRY, 2: 'minecraft:dirt'}]),
        }
        for label, kw in cases.items():
            with self.subTest(label):
                report = self.probe(FakeTools(**kw))
                self.assertFalse(report['ok'])
                self.assertTrue(report['fatal'])
                self.assert_clean(report)
        report = self.probe(FakeTools(fingerprints=[GOOD, 'd' * 64]))
        self.assertEqual(report['tiles'], {}, 'nothing rendered from a copy that differs from the live input')

    def test_visible_index_changes_identity_and_mismatch(self):
        changed = index(sha='8' * 64)
        changed['tiles']['1,0'] = 'f' * 64
        report = self.probe(FakeTools(indexes=[index(), changed]))
        self.assertFalse(report['ok'])
        self.assertEqual(report['tiles']['1,0']['result'], 'changed')
        self.assertEqual(report['counts'].get('changed'), 1)

        unrelated = index(sha='7' * 64)
        unrelated['tiles']['7,7'] = 'f' * 64              # a newer publication elsewhere in the world
        report = self.probe(FakeTools(indexes=[index(), unrelated]))
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['visible']['finalIndexSha256'], '7' * 64)

        tools = FakeTools(indexes=[index(identity='9' * 64)])
        report = self.probe(tools)
        self.assertIn('identity', report['fatal'])
        self.assertEqual(tools.copy_calls, 0, 'refused before copying')

        tools = FakeTools(indexes=[index(atlas='8' * 64)])
        self.assertIn('atlas', self.probe(tools)['fatal'])

        report = self.probe(FakeTools(indexes=[index(), index(atlas='8' * 64)]))
        self.assertIn('atlas', report['fatal'], 'final atlas change is fatal')

        wrong = index()
        wrong['tiles']['0,1'] = '0' * 64
        report = self.probe(FakeTools(indexes=[wrong, wrong]))
        self.assertFalse(report['ok'])
        self.assertEqual(report['tiles']['0,1']['result'], 'mismatch')

        report = self.probe(FakeTools(indexes=[index(stale={'0,0'}), index(stale={'0,0'})]))
        self.assertFalse(report['ok'])
        self.assertEqual(report['tiles']['0,0']['result'], 'stale')

    def test_scratch_is_bounded_by_budget_and_free_space(self):
        tools = FakeTools()
        report = self.probe(tools, budget_bytes=4 * REGION_FILE_BYTES)
        self.assertFalse(report['ok'])
        self.assertIn('budget', report['fatal'])
        self.assertEqual(report['tiles'], {})
        self.assert_clean(report)

        tools = FakeTools()
        report = self.probe(tools, disk_free=lambda p: 10 * 1024 ** 3)
        self.assertIn('free disk', report['fatal'])
        self.assertEqual(tools.copy_calls, 0)
        self.assert_clean(report)

    def test_tiles_outside_the_region_are_refused(self):
        report = self.probe(FakeTools(), tiles=[(8, 0)])
        self.assertFalse(report['ok'])
        self.assertIn('outside', report['fatal'])

    def test_staging_callback_only_for_matches_and_is_provisional(self):
        calls = []
        def stage(key, volume, mesh):
            calls.append((key, volume.x0, mesh['chunks'][(0, 0)]['origin']))
            return {'pairSha256': 'a' * 64}
        report = self.probe(FakeTools(), tiles=[(0, 0)], on_match=stage)
        self.assertTrue(report['ok'])
        self.assertEqual(calls, [('0,0', 0, (0, 0))])
        self.assertEqual(report['tiles']['0,0']['staged']['pairSha256'], 'a' * 64)
        calls.clear()
        report = self.probe(FakeTools(indexes=[index(stale=['0,0']), index(stale=['0,0'])]),
                            tiles=[(0, 0)], on_match=stage)
        self.assertFalse(report['ok'])
        self.assertEqual(calls, [])
        report = self.probe(FakeTools(fingerprints=[GOOD, GOOD, 'e' * 64]),
                            tiles=[(0, 0)], on_match=stage)
        self.assertFalse(report['ok'], 'staged objects never override final source check')
        self.assertIn('staged', report['tiles']['0,0'])
        self.assert_clean(report)

    def test_staging_exception_refuses_region_and_cleans_scratch(self):
        def fail(*args):
            raise ValueError('staging budget exceeded')
        report = self.probe(FakeTools(), tiles=[(0, 0)], on_match=fail)
        self.assertFalse(report['ok'])
        self.assertEqual(report['tiles']['0,0']['result'], 'error')
        self.assertIn('staging budget', report['tiles']['0,0']['reason'])
        self.assert_clean(report)

    def test_cli_rejects_invalid_arguments_with_nonzero_exit(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = crp.main(['--world', str(self.world), '--catalog-dir', str(self.world), '--hires-store',
                             str(self.world), '--scratch-root', str(self.scratch_root), '--region', 'x'])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out.getvalue())['ok'])

    # ------------------------------------------------------------------ published immutable V3 index

    def test_published_v3_index_is_used_even_when_the_legacy_index_differs(self):
        store = self.root / 'hires'
        sha = write_store(store, all_tiles())
        legacy = {'format': 'UIE_HIRES_INDEX1', 'tileSize': 64, 'identity': IDENTITY, 'atlasSha256': ATLAS,
                  'caves': False, 'tiles': {k: 'f' * 12 for k in all_tiles()}}
        (store / 'index.json').write_text(json.dumps(legacy))     # producer ahead of the publisher
        visible = crp.read_visible(store)
        self.assertEqual(visible['indexSha256'], sha)
        self.assertEqual(visible['tiles']['1,0'], visible_sha(1, 0), 'full SHA-256 from the V3 index')
        report = self.probe(FakeTools(read_index=crp.read_visible), hires_store=store)
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['visible']['indexSha256'], sha)
        self.assertEqual({e['result'] for e in report['tiles'].values()}, {'match'})

        (store / 'current.json').unlink()                         # only the legacy index left: no fallback
        report = self.probe(FakeTools(read_index=crp.read_visible), hires_store=store)
        self.assertIn('current.json is missing', report['fatal'])

    def test_published_v3_stale_and_world_arguments(self):
        store = self.root / 'hires'
        write_store(store, all_tiles(), stale=['0,0'])
        visible = crp.read_visible(store)
        self.assertEqual(visible['stale'], {'0,0'})
        self.assertFalse(visible['planComplete'])
        with self.assertRaisesRegex(crp.ProbeError, 'surface of world'):
            crp.read_visible(store, world='other')
        with self.assertRaisesRegex(crp.ProbeError, 'surface of world'):
            crp.read_visible(store, dimension='-1')

    def test_unpublished_or_malformed_v3_states_are_refused(self):
        tiles = all_tiles()
        cases = {
            'V2 index': dict(fmt='UIE_HIRES_IMMUTABLE_INDEX2'),
            'cave index': dict(context={**CONTEXT, 'caves': True}),
            'other world': dict(context={**CONTEXT, 'world': 'creative'}),
            'context field missing': dict(context={k: v for k, v in CONTEXT.items() if k != 'renderer'}),
            'extra field': dict(extra={'note': 'x'}),
            'not canonical': dict(canonical=False),
            'stale unsorted': dict(stale=['1,0', '0,0']),
            'stale unknown tile': dict(stale=['9,9']),
            'bad pointer format': dict(pointer={'format': 'OTHER', 'indexSha256': 'a' * 64}),
            'pointer to missing index': dict(pointer={'format': 'UIE_HIRES_POINTER1', 'indexSha256': 'a' * 64}),
        }
        for label, kw in cases.items():
            with self.subTest(label):
                store = self.root / f'store-{len(label)}-{abs(hash(label))}'
                write_store(store, tiles, **kw)
                with self.assertRaises(crp.ProbeError):
                    crp.read_visible(store)

        store = self.root / 'tampered'
        sha = write_store(store, tiles)
        path = store / 'indexes' / f'{sha}.json'
        path.write_bytes(path.read_bytes().replace(b'"tileSize":64', b'"tileSize":65'))
        with self.assertRaisesRegex(crp.ProbeError, 'does not match its hash'):
            crp.read_visible(store)

        store = self.root / 'duplicate'
        (store / 'indexes').mkdir(parents=True)
        raw = b'{"format":"UIE_HIRES_POINTER1","indexSha256":"' + b'a' * 64 + b'","format":"UIE_HIRES_POINTER1"}'
        (store / 'current.json').write_bytes(raw)
        with self.assertRaisesRegex(crp.ProbeError, 'duplicate'):
            crp.read_visible(store)

        store = self.root / 'linked'
        sha = write_store(store, tiles)
        target = store / 'elsewhere.json'
        (store / 'indexes' / f'{sha}.json').rename(target)
        try:
            (store / 'indexes' / f'{sha}.json').symlink_to(target)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(crp.ProbeError):
            crp.read_visible(store)


class ToolkitTests(unittest.TestCase):
    def test_identity_uses_canonical_surface_formula_without_geometry_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, catalog_dir, other = root / 'code', root / 'catalog', root / 'other'
            for d in (code, catalog_dir, other):
                d.mkdir()
            sources = {'offline_mesher.py': b'mesher bytes', 'offline_catalog.py': b'catalog bytes',
                       'offline_volume.py': b'volume bytes'}
            for name, data in sources.items():
                (code / name).write_bytes(data)
            (catalog_dir / 'catalog.json').write_text('{}')
            (catalog_dir / 'atlas.png').write_bytes(b'atlas bytes')
            loaded = []

            def load_compiled(path):
                loaded.append(Path(path).name)
                return SimpleNamespace(identity='catalog-identity')

            def modules(where):
                return {'capture_batch': SimpleNamespace(),
                        'offline_catalog': SimpleNamespace(__file__=str(where / 'offline_catalog.py'),
                                                           Catalog=SimpleNamespace(load_compiled=load_compiled)),
                        'offline_mesher': SimpleNamespace(__file__=str(code / 'offline_mesher.py'),
                                                          Mesher=lambda catalog, colormap, caves: ('mesher', caves),
                                                          Colormap=lambda grass, foliage: None),
                        'offline_volume': SimpleNamespace(__file__=str(code / 'offline_volume.py'), Volume=object)}

            saved = wh.__dict__.pop('geometry_identity', None)        # like the deployed collision-code copy
            try:
                tools = crp.Toolkit(catalog_dir, modules=modules(code), code_dir=code)
                with self.assertRaisesRegex(crp.ProbeError, 'not imported from'):
                    crp.Toolkit(catalog_dir, modules=modules(other), code_dir=code)
            finally:
                if saved is not None:
                    wh.geometry_identity = saved
            joined = wh.MAGIC + sources['offline_mesher.py'] + sources['offline_catalog.py'] + sources['offline_volume.py']
            expected = hashlib.sha256(b'catalog-identity' + hashlib.sha256(joined).digest()).hexdigest()
            self.assertEqual(tools.identity, expected)
            self.assertEqual(tools.atlas, hashlib.sha256(b'atlas bytes').hexdigest())
            self.assertEqual(loaded[0], 'catalog.json')
            self.assertEqual(tools._mesher, ('mesher', False), 'surface mesher, never caves')
            if saved is not None:       # newer world_hires: same value as its surface identity
                self.assertEqual(expected, saved('catalog-identity', joined, False, False))


if __name__ == '__main__':
    unittest.main()

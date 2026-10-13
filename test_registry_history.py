"""Registry history safety with the REAL capture_batch.from_chunks (no stub) through world_hires._batch.

A worker keeps world_hires._W["registry"] for its whole life and _batch extends it with "uie:unknown_<id>" for ids
missing from level.dat. Whatever a worker rendered before must not change what a later batch says about the blocks it
really uses. The tests compare used cells, their names and output bytes (batch body, UIB1 sidecar). They never compare
whole registries: legitimate placeholders of earlier batches may stay in the worker registry and the volume.

Synthetic chunks (reader.Chunk with blocks16 sections), tiles 0,0 and 1,0 with a 16 block halo; no world, catalog,
mesher or files. Run from the work directory (root runs Linux):
    python3 -m unittest discover -s uie-map -p "test_registry_history.py" -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capture_batch as cb  # noqa: E402
import collision_tile as ct  # noqa: E402
import reader  # noqa: E402
import world_hires as wh  # noqa: E402
from offline_volume import Volume  # noqa: E402

AIR, STONE, PORTAL = 'minecraft:air', 'minecraft:stone', 'minecraft:portal'
REGISTRY = {0: AIR, 1: STONE, 90: PORTAL}
PORTAL_CELLS = [(20, 90, 3), (20, 91, 3), (21, 90, 3)]      # tile 0,0, chunk (1,0)
_MISSING = object()


def make_chunk(cx, cz, voxels=(), floor=True):
    """Chunk with stone at y=0 (optional) plus voxels [(block_id, (x, y, z), meta)] in world coordinates."""
    sections = {}

    def section(sy):
        if sy not in sections:
            sections[sy] = (bytearray(8192), bytearray(2048))
        return sections[sy]

    def put(x, y, z, block_id, meta):
        assert x >> 4 == cx and z >> 4 == cz and 0 <= y < 256
        b16, data = section(y >> 4)
        i = ((y & 15) << 8) | ((z & 15) << 4) | (x & 15)
        b16[2 * i], b16[2 * i + 1] = block_id >> 8, block_id & 0xFF
        data[i >> 1] = (data[i >> 1] & 0x0F) | (meta << 4) if i & 1 else (data[i >> 1] & 0xF0) | meta

    if floor:
        for z in range(cz * 16, cz * 16 + 16):
            for x in range(cx * 16, cx * 16 + 16):
                put(x, 0, z, 1, 0)
    for block_id, (x, y, z), *meta in voxels:
        put(x, y, z, block_id, meta[0] if meta else 0)
    return reader.Chunk(cx, cz, {sy: reader.Section(sy, None, bytes(d), None, bytes(b))
                                 for sy, (b, d) in sections.items()}, bytes(256))


def portal_window():
    """Tile 0,0: a portal frame of known ids only."""
    return {(0, 0): make_chunk(0, 0),
            (1, 0): make_chunk(1, 0, [(90, c, 0) for c in PORTAL_CELLS]),
            (0, 1): make_chunk(0, 1, [(1, (3, 90, 20), 2)])}


def unknown_window(entries):
    """Tile 1,0: unknown ids at the given world positions (all inside chunk (4,0))."""
    return {(4, 0): make_chunk(4, 0, [(block_id, at) for block_id, at in entries])}


# Histories that a worker may have rendered before the portal tile. Every id is absent from REGISTRY and placed
# at a y equal to a KNOWN id (0 = air, 1 = stone, 90 = portal); "many" needs two repair rounds (message lists 10).
HISTORIES = {
    'y90': [(999, (69, 90, 7))],
    'y1': [(1000, (70, 1, 8))],
    'y0': [(1001, (71, 0, 9))],
    'two-ids': [(4090, (72, 90, 10)), (4001, (73, 1, 11))],
    'many': [(2000 + i, (64 + i, (0, 1, 90)[i % 3], 12)) for i in range(12)],
}


def legacy_ids(message):
    """RECONSTRUCTION of the pre-fix parsing (split on ', ', every leading digit token is an id) used only to prove
    that these fixtures detect it; this is not the frozen production code."""
    found = []
    for part in message.split('missing from the registry: ', 1)[1].split(', '):
        token = part.split(' ', 1)[0]
        if token.isdigit():
            found.append(int(token))
    return found


def legacy_batch(area, core, window):
    for _ in range(8):
        try:
            return cb.from_chunks(area, core, wh._W['registry'], window)
        except cb.BatchError as error:
            if 'missing from the registry' not in str(error):
                raise
            for block_id in legacy_ids(str(error)):
                wh._W['registry'][block_id] = f'uie:unknown_{block_id}'
    raise wh.HiresError('too many unknown block ids')


class RegistryHistoryTests(unittest.TestCase):
    def setUp(self):
        saved = wh._W.get('registry', _MISSING)

        def restore():
            if saved is _MISSING:
                wh._W.pop('registry', None)
            else:
                wh._W['registry'] = saved
        self.addCleanup(restore)
        self.cold_worker()

    @staticmethod
    def cold_worker():
        wh._W['registry'] = dict(REGISTRY)

    @staticmethod
    def render(tx, tz, window, batch_fn=None):
        """Batch -> Volume -> UIB1 for one tile, exactly as the collision probe does (minus meshing)."""
        area, core = wh.tile_area(tx, tz)
        batch = (batch_fn or (lambda a, c, w: wh._batch(cb, a, c, w)))(area, core, window)
        volume = Volume(batch)
        return batch, volume, ct.encode_raw(volume, tx * wh.TILE, tz * wh.TILE)

    def used_names(self, volume, cells):
        return [(volume.block(*c)[0], volume.block(*c)[1], volume.registry[volume.block(*c)[0]]) for c in cells]

    # ------------------------------------------------------------------ the fixture really uses the real format

    def test_fixture_produces_the_real_missing_id_message(self):
        area, core = wh.tile_area(1, 0)
        with self.assertRaises(cb.BatchError) as raised:
            cb.from_chunks(area, core, REGISTRY, unknown_window(HISTORIES['y90']))
        self.assertTrue(str(raised.exception).endswith('missing from the registry: 999 at (69, 90, 7)'),
                        str(raised.exception))

    def test_fixture_detects_the_legacy_parser(self):
        """Sanity of the fixtures: the reconstructed pre-fix parsing renames portal 90 and changes the UIB1 bytes."""
        _, _, cold = self.render(0, 0, portal_window(), legacy_batch)
        self.render(1, 0, unknown_window(HISTORIES['y90']), legacy_batch)
        self.assertEqual(wh._W['registry'][90], 'uie:unknown_90')
        _, volume, warm = self.render(0, 0, portal_window(), legacy_batch)
        self.assertEqual(volume.registry[90], 'uie:unknown_90')
        self.assertNotEqual(warm, cold)

    # ------------------------------------------------------------------ real _batch

    def test_unknown_id_at_y90_in_the_same_batch_keeps_portal(self):
        window = dict(portal_window())
        window[(4, 0)] = unknown_window(HISTORIES['y90'])[(4, 0)]     # halo chunk of tile 0,0
        batch, volume, _ = self.render(0, 0, window)
        self.assertEqual(self.used_names(volume, PORTAL_CELLS), [(90, 0, PORTAL)] * 3)
        self.assertEqual(volume.block(69, 90, 7)[0], 999)
        self.assertEqual(volume.registry[999], 'uie:unknown_999')
        self.assertEqual(volume.registry[0], AIR)
        self.assertEqual(volume.registry[1], STONE)
        self.assertEqual(wh._W['registry'][90], PORTAL)

    def test_known_ids_keep_names_and_bytes_after_any_unknown_history(self):
        cold_batch, cold_volume, cold_uib = self.render(0, 0, portal_window())
        cold_tile = ct.decode_raw(cold_uib)
        self.assertEqual(cold_tile['registry'], {'0': AIR, '1': STONE, '90': PORTAL}, 'UIB1 lists used ids only')
        for name, history in HISTORIES.items():
            with self.subTest(history=name):
                self.cold_worker()
                self.render(1, 0, unknown_window(history))              # the worker's previous job
                for block_id, _ in history:
                    self.assertEqual(wh._W['registry'][block_id], f'uie:unknown_{block_id}')
                for block_id, known in REGISTRY.items():
                    self.assertEqual(wh._W['registry'][block_id], known, f'known id {block_id} after {name}')
                batch, volume, uib = self.render(0, 0, portal_window())
                self.assertEqual(batch.body, cold_batch.body)
                self.assertEqual(self.used_names(volume, PORTAL_CELLS + [(3, 90, 20), (0, 0, 0), (5, 200, 5)]),
                                 self.used_names(cold_volume, PORTAL_CELLS + [(3, 90, 20), (0, 0, 0), (5, 200, 5)]))
                self.assertEqual(uib, cold_uib, 'UIB1 bytes identical to a cold worker')
                tile = ct.decode_raw(uib)
                self.assertEqual(ct.block_at(tile, 20, 90, 3), (90, 0, PORTAL))
                self.assertEqual(ct.block_at(tile, 3, 90, 20), (1, 2, STONE))

    def test_unknown_tile_output_does_not_depend_on_earlier_unknown_tiles(self):
        target = HISTORIES['many']
        _, _, cold_uib = self.render(1, 0, unknown_window(target))
        for name in ('y90', 'y1', 'y0', 'two-ids'):
            with self.subTest(previous=name):
                self.cold_worker()
                self.render(1, 0, unknown_window(HISTORIES[name]))
                _, volume, uib = self.render(1, 0, unknown_window(target))
                self.assertEqual(uib, cold_uib)
                tile = ct.decode_raw(uib)
                for block_id, at in target:
                    self.assertEqual(ct.block_at(tile, *at), (block_id, 0, f'uie:unknown_{block_id}'))
                self.assertEqual(ct.block_at(tile, 64, 0, 0), (1, 0, STONE))

    def test_repeated_tile_in_one_worker_is_stable(self):
        _, _, first = self.render(0, 0, portal_window())
        self.render(1, 0, unknown_window(HISTORIES['many']))
        _, _, second = self.render(0, 0, portal_window())
        self.render(1, 0, unknown_window(HISTORIES['y90']))
        _, _, third = self.render(0, 0, portal_window())
        self.assertEqual({first, second, third}, {first})


if __name__ == '__main__':
    unittest.main()

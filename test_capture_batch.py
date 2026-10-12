"""Tests for capture_batch.py (UIE2 codec). Pure in-memory fixtures.

Run from the work directory:  python -m unittest discover -s uie-map -p "test_capture_batch.py" -v
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capture_batch as cb  # noqa: E402
import capture_snapshot as cs  # noqa: E402
import reader  # noqa: E402
from capture_batch import BatchError  # noqa: E402

REGISTRY = {0: "minecraft:air", 1: "minecraft:stone", 7: "minecraft:bedrock", 300: "mod:\x00nul\U0001F600",
            12965: "hbm:tile.hadron_coil_gold", 15983: "mod:top"}
COLUMN = 1 + 256 * 3


def block_at(x, y, z):
    if y == 0:
        return 7, 0
    if y <= 3:
        return 1, (x + z + y) & 15
    if y == 4:
        return 12965, x & 15
    if y == 5 and x == z:
        return 300, 9
    if y == 255:
        return 15983, z & 15
    return 0, 0


def biome_at(x, z):
    return (x * 7 + z * 3) & 0xFF


def make_chunk(cx, cz, fn=block_at, biomes=True):
    sections = {}
    for sy in (0, 15):
        b16, data = bytearray(8192), bytearray(2048)
        for ly in range(16):
            for lz in range(16):
                for lx in range(16):
                    bid, meta = fn(cx * 16 + lx, sy * 16 + ly, cz * 16 + lz)
                    i = (ly << 8) | (lz << 4) | lx
                    b16[2 * i], b16[2 * i + 1] = bid >> 8, bid & 0xFF
                    data[i >> 1] |= meta << 4 if i & 1 else meta
        sections[sy] = reader.Section(sy, None, bytes(data), None, bytes(b16))
    bio = bytes(biome_at(cx * 16 + i % 16, cz * 16 + i // 16) for i in range(256)) if biomes else None
    return reader.Chunk(cx, cz, sections, bio)


# Input X -48..15, Z -32..15 (chunks x -3..0, z -2..0); core X -32..-1, Z -16..-1 with a 16-block halo.
AREA = cs.Area(-48, -32, 64, 48)
CORE = cs.Area(-32, -16, 32, 16)
WINDOW = [(x, z) for z in (-2, -1, 0) for x in (-3, -2, -1, 0)]
ABSENT = {(-3, -2), (0, 0)}
_CHUNK_CACHE = {}


def chunks(present=None):
    keys = [c for c in WINDOW if c not in ABSENT] if present is None else present
    for key in keys:
        if key not in _CHUNK_CACHE:
            _CHUNK_CACHE[key] = make_chunk(*key)
    return {key: _CHUNK_CACHE[key] for key in keys}


def expected_column(x, z):
    return bytes([biome_at(x, z)]) + b"".join(struct.pack(">HB", *block_at(x, y, z)) for y in range(256))


# ---------------------------------------------------------------- independent wire builder for negative tests

SMALL = cs.Area(0, 0, 48, 48)       # chunks 0..2 x 0..2
SMALL_CORE = cs.Area(16, 16, 16, 16)  # chunk (1,1)


def raw_batch(area=SMALL, core=SMALL_CORE, flags=None, registry=None, body=None, presence_count=None,
              registry_count=None, magic=b"UIE2"):
    flags = [1] * 9 if flags is None else flags
    registry = [(0, b"minecraft:air"), (1, b"minecraft:stone")] if registry is None else registry
    if body is None:
        stone = bytes([4]) + struct.pack(">HB", 1, 3) * 256
        cols = []
        for z in range(area.z, area.z + area.depth):
            for x in range(area.x, area.x + area.width):
                i = ((z - area.z) // 16) * (area.width // 16) + (x - area.x) // 16
                cols.append(stone if flags[i] else bytes(COLUMN))
        body = b"".join(cols)
    out = [magic, struct.pack(">5i", area.x, area.z, area.width, area.depth,
                              len(registry) if registry_count is None else registry_count),
           struct.pack(">4i", core.x, core.z, core.width, core.depth),
           struct.pack(">i", len(flags) if presence_count is None else presence_count), bytes(flags)]
    for block_id, name in registry:
        out += [struct.pack(">iH", block_id, len(name)), name]
    return b"".join(out) + body


def set_voxel(data, area, x, y, z, block_id, meta):
    body_start = len(data) - area.width * area.depth * COLUMN  # the body is the final section
    pos = body_start + ((z - area.z) * area.width + (x - area.x)) * COLUMN + 1 + y * 3
    return data[:pos] + struct.pack(">HB", block_id, meta) + data[pos + 3:]


class RoundtripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = cb.from_chunks(AREA, CORE, REGISTRY, chunks())
        cls.data = cb.encode_batch(cls.snapshot)

    def test_roundtrip(self):
        parsed = cb.parse_batch(self.data)
        self.assertEqual((parsed.area, parsed.core, parsed.registry), (AREA, CORE, REGISTRY))
        self.assertEqual(parsed.present_chunks, frozenset(set(WINDOW) - ABSENT))
        self.assertEqual(parsed.body, self.snapshot.body)
        self.assertEqual(cb.encode_batch(parsed), self.data)

    def test_wire_header_and_mask_order(self):
        d = self.data
        self.assertEqual(d[:4], b"UIE2")
        self.assertEqual(struct.unpack_from(">5i", d, 4), (-48, -32, 64, 48, len(REGISTRY)))
        self.assertEqual(struct.unpack_from(">4i", d, 24), (-32, -16, 32, 16))
        self.assertEqual(struct.unpack_from(">i", d, 40), (12,))
        # z-major: z=-2: x -3(absent) -2 -1 0; z=-1: all; z=0: x -3 -2 -1 0(absent)
        self.assertEqual(list(d[44:56]), [0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0])
        self.assertEqual(struct.unpack_from(">iH", d, 56), (0, len(b"minecraft:air")))
        self.assertEqual(len(d) - AREA.width * AREA.depth * COLUMN,
                         56 + sum(6 + len(cs.encode_mutf8(n)) for n in REGISTRY.values()))

    def test_present_columns_hold_real_data(self):
        body = self.snapshot.body
        for x, z in [(-47, -1), (-32, -16), (-1, -1), (-17, -17), (15, -20), (-40, 15), (-20, -20)]:
            pos = ((z - AREA.z) * AREA.width + (x - AREA.x)) * COLUMN
            self.assertEqual(body[pos:pos + COLUMN], expected_column(x, z), f"column {x},{z}")
            biome, voxels = self.snapshot.column(x, z)
            self.assertEqual(biome, biome_at(x, z))
            self.assertEqual(voxels[4], (12965, x & 15))
            self.assertEqual(voxels[255], (15983, z & 15))
        self.assertEqual(self.snapshot.column(-20, -20)[1][5], (300, 9))

    def test_absent_columns_are_zero_and_refused(self):
        body = self.snapshot.body
        for x, z in [(0, 0), (15, 15), (-48, -32), (-33, -17)]:
            pos = ((z - AREA.z) * AREA.width + (x - AREA.x)) * COLUMN
            self.assertEqual(body[pos:pos + COLUMN], bytes(COLUMN))
            self.assertFalse(self.snapshot.present_at(x, z))
            with self.assertRaises(BatchError):
                self.snapshot.column(x, z)
        self.assertTrue(self.snapshot.present_at(-32, -16))
        for x, z in [(-49, 0), (16, 0), (0, -33), (0, 16)]:
            self.assertFalse(self.snapshot.present_at(x, z))
            with self.assertRaises(BatchError):
                self.snapshot.column(x, z)


class AreaTests(unittest.TestCase):
    def test_alignment_and_halo(self):
        present = chunks()
        cases = {
            "input x unaligned": (cs.Area(-47, -32, 64, 48), CORE),
            "input width unaligned": (cs.Area(-48, -32, 63, 48), CORE),
            "core unaligned": (AREA, cs.Area(-31, -16, 32, 16)),
            "core depth 8": (AREA, cs.Area(-32, -16, 32, 8)),
            "no west halo": (AREA, cs.Area(-48, -16, 32, 16)),
            "no east halo": (AREA, cs.Area(-16, -16, 32, 16)),
            "no north halo": (AREA, cs.Area(-32, -32, 32, 16)),
            "no south halo": (AREA, cs.Area(-32, 0, 32, 16)),
            "core outside": (AREA, cs.Area(64, -16, 16, 16)),
        }
        for label, (area, core) in cases.items():
            with self.subTest(label), self.assertRaises(BatchError):
                cb.from_chunks(area, core, REGISTRY, present)
        with self.assertRaises(cs.SnapshotError):
            cs.Area(0, 0, 144, 48)  # the input can never exceed 128 blocks
        cb.from_chunks(cs.Area(-48, -32, 48, 48), cs.Area(-32, -16, 16, 16), REGISTRY,
                       {k: v for k, v in present.items() if k[0] < 0})

    def test_128_by_128_input(self):
        data = raw_batch(area=cs.Area(-64, -64, 128, 128), core=cs.Area(-48, -48, 96, 96), flags=[1] * 64)
        parsed = cb.parse_batch(data)
        self.assertEqual(len(parsed.present_chunks), 64)


class FromChunksTests(unittest.TestCase):
    def test_core_without_present_chunk(self):
        halo_only = {k: v for k, v in chunks().items() if k not in {(-2, -1), (-1, -1)}}
        with self.assertRaisesRegex(BatchError, "core"):
            cb.from_chunks(AREA, CORE, REGISTRY, halo_only)

    def test_invalid_chunks(self):
        good = chunks()
        cases = {
            "outside window": {**good, (1, 0): make_chunk(1, 0)},
            "wrong position": {**good, (0, 0): make_chunk(-1, -2)},
            "bool key": {(True, 0): make_chunk(1, 0)},
            "no biomes": {**good, (0, 0): make_chunk(0, 0, biomes=False)},
            "not a chunk": {**good, (0, 0): "chunk"},
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(BatchError):
                cb.from_chunks(AREA, CORE, REGISTRY, value)

    def test_unknown_ids_and_registry(self):
        without_hbm = {k: v for k, v in REGISTRY.items() if k != 12965}
        with self.assertRaisesRegex(BatchError, "12965"):
            cb.from_chunks(AREA, CORE, without_hbm, chunks())
        for registry in ({**REGISTRY, 0: "mod:void"}, {k: v for k, v in REGISTRY.items() if k}, {},
                         {**REGISTRY, 70000: "mod:x"}, {**REGISTRY, 2: ""}):
            with self.subTest(registry=sorted(registry)[:3]), self.assertRaises(BatchError):
                cb.from_chunks(AREA, CORE, registry, chunks())

    def test_missing_chunk_is_not_air(self):
        snap = cb.from_chunks(AREA, CORE, REGISTRY, chunks([(-2, -1)]))
        self.assertEqual(snap.present_chunks, frozenset({(-2, -1)}))
        self.assertEqual(snap.column(-32, -16)[1][0], (7, 0))
        with self.assertRaises(BatchError):
            snap.column(-16, -16)  # chunk (-1,-1) is inside the core but absent


class ParseValidationTests(unittest.TestCase):
    def test_minimal_valid(self):
        parsed = cb.parse_batch(raw_batch())
        self.assertEqual(parsed.column(20, 20)[1][0], (1, 3))

    def test_absent_nonzero_body_rejected(self):
        flags = [1] * 9
        flags[0] = 0
        data = raw_batch(flags=flags)
        cb.parse_batch(data)
        for voxel in [(0, 0, 0, 1, 0), (15, 255, 15, 0, 1)]:
            with self.subTest(voxel=voxel), self.assertRaisesRegex(BatchError, "absent"):
                cb.parse_batch(set_voxel(data, SMALL, *voxel))
        biome = bytearray(data)
        biome[len(data) - SMALL.width * SMALL.depth * COLUMN] = 1  # biome byte of absent column (0,0)
        with self.assertRaisesRegex(BatchError, "absent"):
            cb.parse_batch(bytes(biome))

    def test_core_must_have_a_present_chunk(self):
        flags = [1] * 9
        flags[4] = 0  # chunk (1,1) is the whole core
        with self.assertRaisesRegex(BatchError, "core"):
            cb.parse_batch(raw_batch(flags=flags))

    def test_flags_and_counts(self):
        cases = {
            "flag 2": raw_batch(flags=[1, 1, 1, 1, 1, 1, 1, 1, 2]),
            "presence count short": raw_batch(flags=[1] * 8, body=bytes(0)),
            "presence count field": raw_batch(presence_count=10),
            "registry count 0": raw_batch(registry_count=0),
            "registry count high": raw_batch(registry_count=65537),
            "magic": raw_batch(magic=b"UIE1"),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(BatchError):
                cb.parse_batch(data)

    def test_truncated_and_trailing(self):
        data = raw_batch()
        for n in [0, 3, 4, 20, 40, 43, 44, 52, 60, 80, len(data) - 1]:
            with self.subTest(length=n), self.assertRaisesRegex(BatchError, "truncated|not a UIE2"):
                cb.parse_batch(data[:n])
        with self.assertRaisesRegex(BatchError, "trailing"):
            cb.parse_batch(data + b"\x00")

    def test_registry_rejections(self):
        cases = {
            "no air": [(1, b"minecraft:stone")],
            "air renamed": [(0, b"mod:void"), (1, b"minecraft:stone")],
            "duplicate": [(0, b"minecraft:air"), (1, b"minecraft:stone"), (1, b"mod:x")],
            "id too high": [(0, b"minecraft:air"), (1, b"minecraft:stone"), (65536, b"mod:x")],
            "negative id": [(0, b"minecraft:air"), (1, b"minecraft:stone"), (-1, b"mod:x")],
            "bad mutf8": [(0, b"minecraft:air"), (1, b"minecraft:stone"), (2, b"\xf0\x9f\x98\x80")],
        }
        for label, registry in cases.items():
            with self.subTest(label), self.assertRaises(BatchError):
                cb.parse_batch(raw_batch(registry=registry))

    def test_present_voxels_validated(self):
        data = raw_batch()
        with self.assertRaisesRegex(BatchError, "metadata"):
            cb.parse_batch(set_voxel(data, SMALL, 20, 7, 20, 1, 16))
        with self.assertRaisesRegex(BatchError, "missing from the registry"):
            cb.parse_batch(set_voxel(data, SMALL, 20, 7, 20, 2, 0))
        cb.parse_batch(set_voxel(data, SMALL, 20, 7, 20, 0, 0))

    def test_encode_validates(self):
        good = cb.parse_batch(raw_batch())
        bad = [
            cb.BatchSnapshot(good.area, good.core, {**good.registry, 0: "mod:void"}, good.body, good.present_chunks),
            cb.BatchSnapshot(good.area, good.core, good.registry, good.body[:-1], good.present_chunks),
            cb.BatchSnapshot(good.area, good.core, good.registry, good.body, good.present_chunks | {(9, 9)}),
            cb.BatchSnapshot(good.area, good.core, good.registry, good.body, set(good.present_chunks)),
            cb.BatchSnapshot(good.area, good.core, good.registry, good.body, good.present_chunks - {(0, 0)}),
        ]
        for i, snapshot in enumerate(bad):
            with self.subTest(case=i), self.assertRaises(BatchError):
                cb.encode_batch(snapshot)


class Uie1StillWorks(unittest.TestCase):
    def test_uie1_parser_rejects_uie2_and_keeps_working(self):
        with self.assertRaises(cs.SnapshotError):
            cs.parse_snapshot(raw_batch())
        body = bytes(16 * 16 * COLUMN)
        cs.parse_snapshot(cs.encode_snapshot(cs.Area(0, 0, 16, 16), {0: "minecraft:air"}, body))


if __name__ == "__main__":
    unittest.main()

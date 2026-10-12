"""Tests for capture_snapshot.py using synthetic worlds written to a temporary directory.

Run from the work directory:  python -m unittest discover -s uie-map -p "test_capture_snapshot.py" -v
"""
import contextlib
import gzip
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capture_snapshot as cs  # noqa: E402
import reader  # noqa: E402
from capture_snapshot import Area, SnapshotError  # noqa: E402

REGISTRY = {0: "minecraft:air", 1: "minecraft:stone", 7: "minecraft:bedrock",
            12965: "hbm:tile.hadron_coil_gold", 15983: "mod:top"}


def expected_block(x, y, z):
    """Deterministic world content: every layer depends on world X/Z, so a misplaced column shows."""
    if y == 0:
        return 7, 0
    if y <= 3:
        return 1, (x + z + y) & 15
    if y == 4:
        return 12965, x & 15
    if y == 255:
        return 15983, z & 15
    return 0, 0


def expected_biome(x, z):
    return (x * 7 + z * 3) & 0xFF


# ---------------------------------------------------------------- synthetic world writer

T_BYTE, T_INT, T_BARR, T_STR, T_LIST, T_COMP = 1, 3, 7, 8, 9, 10


def _s(text):
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _payload(tag, value):
    if tag == T_BYTE:
        return struct.pack(">b", value)
    if tag == T_INT:
        return struct.pack(">i", value)
    if tag == T_BARR:
        return struct.pack(">i", len(value)) + bytes(value)
    if tag == T_STR:
        return _s(value)
    if tag == T_LIST:
        item, items = value
        return struct.pack(">bi", item, len(items)) + b"".join(_payload(item, v) for v in items)
    if tag == T_COMP:
        return b"".join(struct.pack(">b", t) + _s(k) + _payload(t, v) for k, (t, v) in value.items()) + b"\x00"
    raise AssertionError(tag)


def nbt(compound):
    return b"\x0a" + _s("") + _payload(T_COMP, compound)


def level_dat_bytes(registry):
    items = [{"K": (T_STR, "\x01" + name), "V": (T_INT, bid)} for bid, name in registry.items()]
    items.append({"K": (T_STR, "\x02minecraft:stone"), "V": (T_INT, 1)})
    return gzip.compress(nbt({"Data": (T_COMP, {"version": (T_INT, 19133)}),
                              "FML": (T_COMP, {"ItemData": (T_LIST, (T_COMP, items))})}))


def chunk_bytes(cx, cz, block_fn=expected_block, with_biomes=True):
    sections = []
    for sy in (0, 15):
        blocks16, data = bytearray(8192), bytearray(2048)
        for ly in range(16):
            for lz in range(16):
                for lx in range(16):
                    bid, meta = block_fn(cx * 16 + lx, sy * 16 + ly, cz * 16 + lz)
                    i = (ly << 8) | (lz << 4) | lx
                    blocks16[2 * i], blocks16[2 * i + 1] = bid >> 8, bid & 0xFF
                    data[i >> 1] |= meta << 4 if i & 1 else meta
        sections.append({"Y": (T_BYTE, sy), "Blocks16": (T_BARR, blocks16), "Data": (T_BARR, data)})
    level = {"xPos": (T_INT, cx), "zPos": (T_INT, cz), "Sections": (T_LIST, (T_COMP, sections))}
    if with_biomes:
        level["Biomes"] = (T_BARR, bytes(expected_biome(cx * 16 + i % 16, cz * 16 + i // 16) for i in range(256)))
    return nbt({"Level": (T_COMP, level)})


def region_bytes(records):
    header, body, sector = bytearray(8192), bytearray(), 2
    for (lx, lz), payload in records.items():
        comp = zlib.compress(payload)
        rec = struct.pack(">iB", len(comp) + 1, 2) + comp
        count = -(-len(rec) // 4096)
        struct.pack_into(">I", header, (lx + lz * 32) * 4, (sector << 8) | count)
        body += rec + bytes(count * 4096 - len(rec))
        sector += count
    return bytes(header + body)


def make_world(root, chunks, registry=REGISTRY, **chunk_kwargs):
    """Write level.dat and region files containing exactly the given absolute chunk coords."""
    region_dir = root / "world" / "region"
    region_dir.mkdir(parents=True)
    (root / "world" / "level.dat").write_bytes(level_dat_bytes(registry))
    by_region = {}
    for cx, cz in chunks:
        rx, rz, lx, lz = reader.chunk_to_region(cx, cz)
        by_region.setdefault((rx, rz), {})[(lx, lz)] = chunk_bytes(cx, cz, **chunk_kwargs)
    for (rx, rz), records in by_region.items():
        (region_dir / f"r.{rx}.{rz}.mca").write_bytes(region_bytes(records))
    return root / "world" / "level.dat", region_dir


# Area crossing the X and Z region boundaries at 0: chunks -1/0 lie in regions -1/0.
CROSS_AREA = Area(-2, -1, 4, 3)
CROSS_CHUNKS = [(-1, -1), (0, -1), (-1, 0), (0, 0)]


class WorldTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out_dir = self.root / "out"
        self.out_dir.mkdir()
        self.output = self.out_dir / "uie-capture.bin"

    def tearDown(self):
        self._tmp.cleanup()

    def world(self, chunks=CROSS_CHUNKS, **kwargs):
        self.level, self.regions = make_world(self.root, chunks, **kwargs)

    def run_snapshot(self, area=CROSS_AREA, **kwargs):
        return cs.create_snapshot(self.level, self.regions, area, self.output, **kwargs)

    def assert_nothing_published(self, keep=()):
        self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()), sorted(keep),
                         "no output, provenance or temporary file may remain")


# ---------------------------------------------------------------- pure functions

class MutfTests(unittest.TestCase):
    def test_known_encodings(self):
        cases = {"minecraft:stone": b"minecraft:stone", "\x00": b"\xc0\x80", "é": b"\xc3\xa9",
                 "€": b"\xe2\x82\xac", "\U0001F600": b"\xed\xa0\xbd\xed\xb8\x80", "": b""}
        for text, raw in cases.items():
            with self.subTest(text=text):
                self.assertEqual(cs.encode_mutf8(text), raw)
                self.assertEqual(cs.decode_mutf8(raw), text)

    def test_invalid_or_non_canonical_rejected(self):
        for raw in (b"\x00", b"\xf0\x9f\x98\x80", b"\xc1\x81", b"\xff", b"\xc3"):
            with self.subTest(raw=raw), self.assertRaises(SnapshotError):
                cs.decode_mutf8(raw)

    def test_length_limit(self):
        self.assertEqual(len(cs.encode_mutf8("a" * 65535)), 65535)
        with self.assertRaises(SnapshotError):
            cs.encode_mutf8("a" * 65536)
        with self.assertRaises(SnapshotError):
            cs.encode_mutf8("€" * 21846)  # 65538 encoded bytes


class AreaAndPlanTests(unittest.TestCase):
    def test_area_limits(self):
        Area(-(2 ** 31), 0, 1, 1)
        Area(0, 2 ** 31 - 128, 1, 128)
        for args in ((0, 0, 0, 1), (0, 0, 1, 129), (0, 0, True, 1), (0, 0, 1.0, 1),
                     (2 ** 31 - 1, 0, 2, 1), (0, -(2 ** 31) - 1, 1, 1)):
            with self.subTest(args=args), self.assertRaises(SnapshotError):
                Area(*args)

    def test_negative_and_boundary_selection(self):
        self.assertEqual(cs.plan_chunks(CROSS_AREA), {
            (-1, -1): [(-1, -1, 31, 31)], (0, -1): [(0, -1, 0, 31)],
            (-1, 0): [(-1, 0, 31, 0)], (0, 0): [(0, 0, 0, 0)]})
        self.assertEqual(cs.plan_chunks(Area(-16, 0, 16, 1)), {(-1, 0): [(-1, 0, 31, 0)]})
        self.assertEqual(cs.plan_chunks(Area(-17, 0, 2, 1)), {(-1, 0): [(-2, 0, 30, 0), (-1, 0, 31, 0)]})
        self.assertEqual(cs.plan_chunks(Area(511, 0, 2, 1)), {(0, 0): [(31, 0, 31, 0)], (1, 0): [(32, 0, 0, 0)]})
        self.assertEqual(cs.plan_chunks(Area(-513, -513, 1, 1)), {(-2, -2): [(-33, -33, 31, 31)]})
        full = cs.plan_chunks(Area(0, 0, 128, 128))
        self.assertEqual(list(full), [(0, 0)])
        self.assertEqual(len(full[(0, 0)]), 64)
        self.assertEqual(sum(map(len, cs.plan_chunks(Area(8, 8, 128, 128)).values())), 81)

    def test_encode_parse_roundtrip_with_special_names(self):
        area = Area(-1, 5, 2, 1)
        body = bytes(range(256)) * 6 + bytes(2 * cs.COLUMN_BYTES - 1536)
        registry = {3: "mod:\x00nul", 65535: "mod:é\U0001F600", 0: "minecraft:air"}
        data = cs.encode_snapshot(area, registry, body)
        snap = cs.parse_snapshot(data)
        self.assertEqual((snap.area, snap.registry), (area, registry))
        self.assertEqual(snap.body, body)
        self.assertEqual(data[24:30], struct.pack(">iH", 0, 13))  # entries sorted by ID
        for bad in (data[:-1], data + b"\x00", b"UIE2" + data[4:], data[:20]):
            with self.assertRaises(SnapshotError):
                cs.parse_snapshot(bad)
        with self.assertRaises(SnapshotError):
            cs.encode_snapshot(area, registry, body[:-1])
        with self.assertRaises(SnapshotError):
            cs.encode_snapshot(area, {}, body)


# ---------------------------------------------------------------- end to end

class SnapshotTests(WorldTestCase):
    def test_binary_layout_roundtrip(self):
        self.world()
        self.run_snapshot()
        data = self.output.read_bytes()
        self.assertEqual(data[:4], b"UIE1")
        self.assertEqual(struct.unpack_from(">5i", data, 4), (-2, -1, 4, 3, len(REGISTRY)))
        pos, entries = 24, []
        for _ in range(len(REGISTRY)):
            bid, length = struct.unpack_from(">iH", data, pos)
            entries.append((bid, data[pos + 6:pos + 6 + length].decode("ascii")))
            pos += 6 + length
        self.assertEqual(entries, sorted(REGISTRY.items()))
        self.assertEqual(len(data) - pos, 4 * 3 * 769)
        # First column is (x=-2, z=-1); y=4 holds NEID ID 12965 = 0x32A5 with meta (-2 & 15) = 14.
        self.assertEqual(data[pos], expected_biome(-2, -1))
        self.assertEqual(data[pos + 1 + 4 * 3:pos + 1 + 5 * 3], b"\x32\xa5\x0e")
        self.assertEqual(data[pos + 1 + 255 * 3:pos + 769], b"\x3e\x6f\x0f")  # 15983, meta (-1 & 15)
        for zi in range(3):
            for xi in range(4):
                x, z = -2 + xi, -1 + zi
                col = bytes([expected_biome(x, z)]) + b"".join(
                    struct.pack(">HB", *expected_block(x, y, z)) for y in range(256))
                start = pos + (zi * 4 + xi) * 769
                self.assertEqual(data[start:start + 769], col, f"column x={x} z={z}")

    def test_provenance(self):
        self.world()
        result = self.run_snapshot()
        prov = json.loads(cs.provenance_path(self.output).read_text("utf-8"))
        self.assertEqual(prov, json.loads(json.dumps(result)))
        self.assertEqual(prov["area"], {"x": -2, "z": -1, "width": 4, "depth": 3, "y": [0, 255]})
        self.assertEqual(prov["output"]["sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(prov["levelDat"]["sha256"], hashlib.sha256(self.level.read_bytes()).hexdigest())
        self.assertEqual(sorted(tuple(r["region"]) for r in prov["regions"]), [(-1, -1), (-1, 0), (0, -1), (0, 0)])
        for region in prov["regions"]:
            self.assertEqual(region["sha256"], hashlib.sha256(Path(region["path"]).read_bytes()).hexdigest())
        self.assertEqual(prov["chunks"], {"min": [-1, -1], "max": [0, 0], "count": 4})

    def test_missing_region(self):
        self.world(chunks=[(-1, -1), (0, -1), (-1, 0)])
        with self.assertRaisesRegex(SnapshotError, r"r\.0\.0\.mca"):
            self.run_snapshot()
        self.assert_nothing_published()

    def test_missing_chunk(self):
        self.world(chunks=[(-1, -1), (0, -1), (-1, 0), (1, 0)])  # r.0.0 exists without chunk (0,0)
        with self.assertRaisesRegex(SnapshotError, r"chunk \(0,0\) is not generated"):
            self.run_snapshot()
        self.assert_nothing_published()

    def test_missing_biomes(self):
        self.world(with_biomes=False)
        with self.assertRaisesRegex(SnapshotError, "Biomes"):
            self.run_snapshot()
        self.assert_nothing_published()

    def test_unknown_block_id(self):
        self.world(registry={k: v for k, v in REGISTRY.items() if k != 12965})
        with self.assertRaisesRegex(SnapshotError, r"12965 at \(-2, 4, -1\)"):
            self.run_snapshot()
        self.assert_nothing_published()

    def test_air_may_be_absent_from_registry(self):
        self.world(registry={k: v for k, v in REGISTRY.items() if k != 0})
        self.run_snapshot()
        self.assertNotIn(0, cs.parse_snapshot(self.output.read_bytes()).registry)

    def test_refuses_overwrite_without_force(self):
        self.world()
        self.output.write_bytes(b"old")
        with self.assertRaisesRegex(SnapshotError, "--force"):
            self.run_snapshot()
        self.assertEqual(self.output.read_bytes(), b"old")
        self.assert_nothing_published(keep=["uie-capture.bin"])
        self.run_snapshot(force=True)
        self.assertEqual(self.output.read_bytes()[:4], b"UIE1")

    def test_refuses_existing_provenance_without_force(self):
        self.world()
        cs.provenance_path(self.output).write_text("{}")
        with self.assertRaises(SnapshotError):
            self.run_snapshot()
        self.assertFalse(self.output.exists())

    def test_refuses_input_paths_even_with_force(self):
        self.world()
        original = self.level.read_bytes()
        for target in (self.level, self.regions / "r.0.0.mca", self.regions / "capture.bin"):
            with self.subTest(target=target.name), self.assertRaises(SnapshotError):
                cs.create_snapshot(self.level, self.regions, CROSS_AREA, target, force=True)
        self.assertEqual(self.level.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.regions.iterdir()),
                         ["r.-1.-1.mca", "r.-1.0.mca", "r.0.-1.mca", "r.0.0.mca"])

    def test_source_change_during_read_is_detected(self):
        self.world()
        original = reader.RegionFile.read_chunk
        for victim in (self.regions / "r.0.0.mca", self.level):
            def tampering(region, lx, lz, victim=victim):
                with open(victim, "ab") as fh:
                    fh.write(b"\x00")
                return original(region, lx, lz)
            with self.subTest(victim=victim.name), mock.patch.object(reader.RegionFile, "read_chunk", tampering):
                with self.assertRaisesRegex(SnapshotError, "changed while reading"):
                    self.run_snapshot()
                self.assert_nothing_published()

    def test_failed_replace_keeps_previous_output_and_cleans_temps(self):
        self.world()
        self.output.write_bytes(b"old")
        real_replace = os.replace

        def failing(src, dst):
            if Path(dst) == self.output.resolve():
                raise OSError("simulated rename failure")
            return real_replace(src, dst)

        with mock.patch.object(cs.os, "replace", failing), self.assertRaisesRegex(OSError, "simulated"):
            self.run_snapshot(force=True)
        self.assertEqual(self.output.read_bytes(), b"old")
        self.assertFalse(any(p.suffix == ".tmp" for p in self.out_dir.iterdir()), "temporary files must be removed")

    def test_region_handles_closed_on_success_and_failure(self):
        opened, closed = [], []
        real_init, real_close = reader.RegionFile.__init__, reader.RegionFile.close

        def init(region, *args, **kwargs):
            real_init(region, *args, **kwargs)
            opened.append(region)

        def close(region):
            closed.append(region)
            real_close(region)

        self.world(chunks=[(-1, -1), (0, -1), (-1, 0), (1, 0)])
        with mock.patch.object(reader.RegionFile, "__init__", init), mock.patch.object(reader.RegionFile, "close", close):
            with self.assertRaises(SnapshotError):
                self.run_snapshot()
            self.assertTrue(opened)
            self.assertEqual({id(r) for r in opened}, {id(r) for r in closed})
            self.assertTrue(all(r._fh.closed for r in opened))


class CliTests(WorldTestCase):
    def args(self, *extra):
        return ["--level-dat", str(self.level), "--region-dir", str(self.regions), "--x", "-2", "--z", "-1",
                "--width", "4", "--depth", "3", "--output", str(self.output), *extra]

    def test_success_and_error_exit_codes(self):
        self.world()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cs.main(self.args()), 0)
        self.assertIn("uie-capture.bin", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cs.main(self.args()), 1)
        self.assertIn("--force", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cs.main(self.args("--force")), 0)

    def test_argument_validation(self):
        self.world()
        for bad in (["--width", "129"], ["--depth", "0"], ["--x", "1.5"]):
            argv = self.args()
            flag = bad[0]
            argv[argv.index(flag) + 1] = bad[1]
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                cs.main(argv)
            self.assertEqual(ctx.exception.code, 2)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cs.main(self.args()[:-2] + ["--output", str(self.root / "missing" / "x.bin")]), 1)
        self.assertIn("does not exist", err.getvalue())


if __name__ == "__main__":
    unittest.main()

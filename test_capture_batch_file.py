"""Tests for capture_batch_file.py with a synthetic offline world copy in a temporary directory.

Run from the work directory:  python -m unittest discover -s uie-map -p "test_capture_batch_file.py" -v
"""
import contextlib
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capture_batch as cb  # noqa: E402
import capture_batch_file as cbf  # noqa: E402
import capture_snapshot as cs  # noqa: E402
import ore_snapshot as ore  # noqa: E402
import reader  # noqa: E402
from test_capture_snapshot import level_dat_bytes, region_bytes  # noqa: E402

GT = 500
REGISTRY = {0: "minecraft:air", 1: "minecraft:stone", 7: "minecraft:bedrock", GT: "gregtech:gt.blockores"}
AREA, CORE = cbf.tile_areas(-1, -1)  # input X/Z -112..15, core -96..-1; chunks -7..0
WINDOW = [(x, z) for z in range(-7, 1) for x in range(-7, 1)]
CORE_CHUNKS = {(x, z) for x in range(-6, 0) for z in range(-6, 0)}
ABSENT = {(0, 0), (-7, -7)}  # region r.0.0 missing entirely; (-7,-7) ungenerated inside r.-1.-1
CORE_ORE, HALO_ORE = (-50, 2, -50), (-105, 2, -50)

# ---------------------------------------------------------------- minimal NBT writer (test utility)

T_BYTE, T_SHORT, T_INT, T_BARR, T_STR, T_LIST, T_COMP = 1, 2, 3, 7, 8, 9, 10


def _s(text):
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _payload(tag, value):
    fmt = {T_BYTE: ">b", T_SHORT: ">h", T_INT: ">i"}
    if tag in fmt:
        return struct.pack(fmt[tag], value)
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


def gt_tile(x, y, z, m, n, m_tag=T_SHORT):
    return {"id": (T_STR, "GT_TileEntity_Ores"), "x": (T_INT, x), "y": (T_INT, y), "z": (T_INT, z),
            "m": (m_tag, m), "n": (T_BYTE, n)}


def chest(x, y, z):
    return {"id": (T_STR, "Chest"), "x": (T_INT, x), "y": (T_INT, y), "z": (T_INT, z),
            "Items": (T_LIST, (T_COMP, [{"Slot": (T_BYTE, 0), "id": (T_SHORT, 264)}]))}


def biome(cx, cz):
    return (cx * 7 + cz * 3) & 0xFF


def chunk_nbt(cx, cz, ore_voxels=(), tiles=(), biomes=True):
    blocks16 = bytearray(8192)
    blocks16[0:512] = struct.pack(">H", 7) * 256      # y=0 bedrock
    blocks16[512:1024] = struct.pack(">H", 1) * 256   # y=1 stone
    for x, y, z in ore_voxels:
        i = (y << 8) | ((z & 15) << 4) | (x & 15)
        blocks16[2 * i:2 * i + 2] = struct.pack(">H", GT)
    level = {"xPos": (T_INT, cx), "zPos": (T_INT, cz),
             "Sections": (T_LIST, (T_COMP, [{"Y": (T_BYTE, 0), "Blocks16": (T_BARR, blocks16),
                                              "Data": (T_BARR, bytes(2048))}])),
             "TileEntities": (T_LIST, (T_COMP, list(tiles)))}
    if biomes:
        level["Biomes"] = (T_BARR, bytes([biome(cx, cz)]) * 256)
    return nbt({"Level": (T_COMP, level)})


def default_tiles():
    return {(-4, -4): [gt_tile(*CORE_ORE, 16089, 1), chest(-50, 3, -50)], (-7, -4): [gt_tile(*HALO_ORE, -5, 0)]}


def default_ores():
    return {(-4, -4): [CORE_ORE], (-7, -4): [HALO_ORE]}


class BatchTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out_parent = self.root / "out"
        self.out_parent.mkdir()
        self.output = self.out_parent / "tile_-1_-1"

    def tearDown(self):
        self._tmp.cleanup()

    def world(self, present=None, tiles=None, ores=None, registry=REGISTRY, no_biomes=()):
        present = [c for c in WINDOW if c not in ABSENT] if present is None else present
        tiles = default_tiles() if tiles is None else tiles
        ores = default_ores() if ores is None else ores
        self.regions = self.root / "world" / "region"
        self.regions.mkdir(parents=True)
        self.level = self.root / "world" / "level.dat"
        self.level.write_bytes(level_dat_bytes(registry))
        by_region = {}
        for cx, cz in present:
            rx, rz, lx, lz = reader.chunk_to_region(cx, cz)
            by_region.setdefault((rx, rz), {})[(lx, lz)] = chunk_nbt(
                cx, cz, ores.get((cx, cz), ()), tiles.get((cx, cz), ()), biomes=(cx, cz) not in no_biomes)
        for (rx, rz), records in by_region.items():
            (self.regions / f"r.{rx}.{rz}.mca").write_bytes(region_bytes(records))

    def run_batch(self):
        return cbf.create_batch(self.level, self.regions, AREA, CORE, self.output)

    def input_state(self):
        return {str(p.relative_to(self.root / "world")): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((self.root / "world").rglob("*")) if p.is_file()}

    def assert_nothing_published(self, keep=()):
        self.assertEqual(sorted(p.name for p in self.out_parent.iterdir()), sorted(keep),
                         "no output directory and no temporary directory may remain")


class SuccessTests(BatchTestCase):
    def test_batch_directory_contents(self):
        self.world()
        before = self.input_state()
        provenance = self.run_batch()
        self.assertEqual(self.input_state(), before, "inputs are never written")
        self.assertEqual(sorted(p.name for p in self.out_parent.iterdir()), [self.output.name], "no temp left")
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),
                         ["COMPLETE", "provenance.json", "snapshot.bin", "snapshot.tiles.json"])

        data = (self.output / "snapshot.bin").read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        batch = cb.parse_batch(data)
        self.assertEqual((batch.area, batch.core), (cs.Area(-112, -112, 128, 128), cs.Area(-96, -96, 96, 96)))
        self.assertEqual(batch.present_chunks, frozenset(set(WINDOW) - ABSENT))
        self.assertEqual(batch.registry, REGISTRY)
        biome_value, voxels = batch.column(-50, -50)
        self.assertEqual((biome_value, voxels[0], voxels[1], voxels[2]), (biome(-4, -4), (7, 0), (1, 0), (GT, 0)))
        self.assertEqual(batch.column(-105, -50)[1][2], (GT, 0))
        for x, z in [(0, 0), (15, 15), (-112, -112), (-97, -97)]:
            self.assertFalse(batch.present_at(x, z))

        sidecar = json.loads((self.output / "snapshot.tiles.json").read_text("utf-8"))
        self.assertEqual(sidecar["format"], "UIE_ORES1")
        self.assertEqual(sidecar["snapshotSha256"], sha)
        self.assertEqual(sidecar["tiles"], [{"x": -105, "y": 2, "z": -50, "m": -5, "n": 0},
                                            {"x": -50, "y": 2, "z": -50, "m": 16089, "n": 1}])
        text = (self.output / "snapshot.tiles.json").read_text("utf-8")
        self.assertNotIn("Items", text)
        self.assertNotIn("Chest", text)

        written = json.loads((self.output / "provenance.json").read_text("utf-8"))
        self.assertEqual(written, json.loads(json.dumps(provenance)))
        self.assertEqual(written["input"], {"x": -112, "z": -112, "width": 128, "depth": 128})
        self.assertEqual(written["core"], {"x": -96, "z": -96, "width": 96, "depth": 96})
        self.assertEqual(written["snapshot"]["sha256"], sha)
        self.assertEqual(written["levelDat"]["sha256"], hashlib.sha256(self.level.read_bytes()).hexdigest())
        states = {tuple(r["region"]): r for r in written["regions"]}
        self.assertEqual(set(states), {(-1, -1), (0, -1), (-1, 0), (0, 0)})
        self.assertEqual(states[(0, 0)]["state"], "absent")
        self.assertNotIn("sha256", states[(0, 0)])
        for key in [(-1, -1), (0, -1), (-1, 0)]:
            path = self.regions / f"r.{key[0]}.{key[1]}.mca"
            self.assertEqual(states[key]["state"], "present")
            self.assertEqual(states[key]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual((states[(-1, -1)]["chunksPresent"], states[(-1, -1)]["chunksAbsent"]), (48, 1))
        self.assertEqual(sorted(map(tuple, written["absentChunks"])), sorted(ABSENT))
        self.assertEqual(len(written["presentChunks"]), 62)
        self.assertEqual(written["gtOres"], {"coreOreBlocks": 1, "coreOreTiles": 1, "windowOreTiles": 2})
        self.assertIn("offline", written["note"])

        complete = json.loads((self.output / "COMPLETE").read_text("utf-8"))
        self.assertEqual(complete["snapshotSha256"], sha)
        for name, digest in complete["files"].items():
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), digest)

    def test_cli_negative_tile(self):
        self.world()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cbf.main(["--level-dat", str(self.level), "--region-dir", str(self.regions),
                             "--tile-x", "-1", "--tile-z", "-1", "--output-dir", str(self.output)])
        self.assertEqual(code, 0)
        self.assertIn("62 present / 2 absent", out.getvalue())
        self.assertTrue((self.output / "COMPLETE").is_file())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cbf.main(["--level-dat", str(self.level), "--region-dir", str(self.regions),
                                       "--tile-x", "-1", "--tile-z", "-1", "--output-dir", str(self.output)]), 1)
        self.assertIn("already exists", err.getvalue())

    def test_tile_areas(self):
        self.assertEqual(cbf.tile_areas(-1, 0), (cs.Area(-112, -16, 128, 128), cs.Area(-96, 0, 96, 96)))
        self.assertEqual(cbf.tile_areas(2, -3), (cs.Area(176, -304, 128, 128), cs.Area(192, -288, 96, 96)))
        for bad in [(True, 0), (0, 1.5), (0, "1")]:
            with self.subTest(bad=bad), self.assertRaises(cbf.BatchFileError):
                cbf.tile_areas(*bad)
        with self.assertRaises(cs.SnapshotError):
            cbf.tile_areas(2 ** 31 // 96, 0)


class RejectionTests(BatchTestCase):
    def test_existing_output_is_never_touched(self):
        self.world()
        self.output.mkdir()
        (self.output / "user.txt").write_text("mine")
        with self.assertRaisesRegex(cbf.BatchFileError, "already exists"):
            self.run_batch()
        self.assertEqual((self.output / "user.txt").read_text(), "mine")
        self.assert_nothing_published(keep=[self.output.name])
        empty = self.out_parent / "empty"
        empty.mkdir()
        with self.assertRaises(cbf.BatchFileError):
            cbf.create_batch(self.level, self.regions, AREA, CORE, empty)
        self.assertEqual(list(empty.iterdir()), [])
        as_file = self.out_parent / "file"
        as_file.write_text("keep")
        with self.assertRaises(cbf.BatchFileError):
            cbf.create_batch(self.level, self.regions, AREA, CORE, as_file)
        self.assertEqual(as_file.read_text(), "keep")

    def test_output_inside_inputs_rejected(self):
        self.world()
        before = self.input_state()
        for target in (self.regions / "batch", self.regions / "a" / "b", self.regions, self.level):
            with self.subTest(target=target), self.assertRaises(cbf.BatchFileError):
                cbf.create_batch(self.level, self.regions, AREA, CORE, target)
        self.assertEqual(self.input_state(), before)
        self.assertEqual(sorted(p.name for p in self.regions.iterdir()), ["r.-1.-1.mca", "r.-1.0.mca", "r.0.-1.mca"])

    def test_symlinked_parent_into_region_dir_rejected(self):
        self.world()
        link = self.out_parent / "link"
        try:
            os.symlink(self.regions, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        before = self.input_state()
        with self.assertRaisesRegex(cbf.BatchFileError, "inside the region directory"):
            cbf.create_batch(self.level, self.regions, AREA, CORE, link / "batch")
        self.assertEqual(self.input_state(), before)
        self.assertEqual(sorted(p.name for p in self.regions.iterdir()), ["r.-1.-1.mca", "r.-1.0.mca", "r.0.-1.mca"])

    def test_areas_checked_before_reading(self):
        self.world()
        cases = {
            "core unaligned": (AREA, cs.Area(-95, -96, 96, 96)),
            "area unaligned": (cs.Area(-112, -111, 128, 112), CORE),
            "no west halo": (AREA, cs.Area(-112, -96, 96, 96)),
            "no south halo": (AREA, cs.Area(-96, -80, 96, 96)),
            "core outside": (AREA, cs.Area(64, -96, 16, 16)),
            "not an Area": (AREA, (-96, -96, 96, 96)),
        }
        with mock.patch.object(cbf, "_read_inputs", side_effect=AssertionError("inputs read before area check")):
            for label, (area, core) in cases.items():
                with self.subTest(label), self.assertRaises(cbf.BatchFileError):
                    cbf.create_batch(self.level, self.regions, area, core, self.output)
        self.assert_nothing_published()

    def test_core_ore_without_tile_entity_fails(self):
        self.world(tiles={(-7, -4): [gt_tile(*HALO_ORE, -5, 0)]})  # core ore voxel (-50,2,-50) has no record
        with self.assertRaisesRegex(cbf.BatchFileError, r"1 GT ore block.*\(-50, 2, -50\)"):
            self.run_batch()
        self.assert_nothing_published()

    def test_halo_ore_without_tile_entity_is_allowed(self):
        self.world(tiles={(-4, -4): [gt_tile(*CORE_ORE, 16089, 1)]})  # halo ore (-105,2,-50) has no record
        provenance = self.run_batch()
        self.assertEqual(provenance["gtOres"], {"coreOreBlocks": 1, "coreOreTiles": 1, "windowOreTiles": 1})
        self.assertTrue((self.output / "COMPLETE").is_file())

    def test_core_without_present_chunk(self):
        self.world(present=[c for c in WINDOW if c not in CORE_CHUNKS], tiles={}, ores={})
        with self.assertRaisesRegex(cb.BatchError, "core"):
            self.run_batch()
        self.assert_nothing_published()

    def test_missing_biomes(self):
        self.world(no_biomes={(-3, -3)})
        with self.assertRaisesRegex(cb.BatchError, "Biomes"):
            self.run_batch()
        self.assert_nothing_published()

    def test_unknown_ids_and_registry_without_air(self):
        self.world(registry={k: v for k, v in REGISTRY.items() if k != GT})
        with self.assertRaisesRegex(cb.BatchError, "500"):
            self.run_batch()
        self.assert_nothing_published()

    def test_registry_without_air(self):
        self.world(registry={k: v for k, v in REGISTRY.items() if k != 0})
        with self.assertRaisesRegex(cb.BatchError, "minecraft:air"):
            self.run_batch()
        self.assert_nothing_published()

    def test_gt_whitelist_and_malformed_records(self):
        cases = {
            "tile on stone": ({(-4, -4): [gt_tile(-50, 1, -50, 1, 1)]}, "minecraft:stone"),
            "m out of range": ({(-4, -4): [gt_tile(*CORE_ORE, 40000, 1, m_tag=T_INT)]}, "m=40000"),
            "n invalid": ({(-4, -4): [gt_tile(*CORE_ORE, 1, 2)]}, "n=2"),
            "wrong chunk": ({(-4, -4): [gt_tile(-20, 2, -50, 1, 1)]}, "outside its chunk"),
        }
        for label, (tiles, pattern) in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.world(tiles=tiles)
                with self.assertRaisesRegex(ore.OreSnapshotError, pattern):
                    self.run_batch()
                self.assert_nothing_published()

    def test_corrupt_existing_region_is_an_error_not_missing(self):
        self.world()
        path = self.regions / "r.0.-1.mca"
        path.write_bytes(path.read_bytes()[:100])
        with self.assertRaises(reader.FormatError):
            self.run_batch()
        self.assert_nothing_published()


class ConsistencyAndPublicationTests(BatchTestCase):
    def tamper(self, action):
        original = reader.RegionFile.read_chunk_nbt

        def hooked(region, lx, lz):
            action()
            return original(region, lx, lz)
        return mock.patch.object(reader.RegionFile, "read_chunk_nbt", hooked)

    def test_source_changes_abort_and_clean_up(self):
        def append(path):
            def action():
                with path.open("ab") as fh:
                    fh.write(b"\x00")
            return action

        cases = {
            "region changed": lambda: append(self.regions / "r.-1.-1.mca"),
            "level.dat changed": lambda: append(self.level),
        }
        for label, make_action in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.world()
                with self.tamper(make_action()), self.assertRaisesRegex(cbf.BatchFileError, "changed while reading"):
                    self.run_batch()
                self.assert_nothing_published()

    def test_absent_region_appearing_after_it_was_read_aborts(self):
        self.world()
        real_verify = cbf._verify_sources

        def appear_then_verify(*args):
            (self.regions / "r.0.0.mca").write_bytes(b"")  # recorded as absent, now present
            real_verify(*args)
        with mock.patch.object(cbf, "_verify_sources", appear_then_verify):
            with self.assertRaisesRegex(cbf.BatchFileError, r"r\.0\.0\.mca \(appeared\)"):
                self.run_batch()
        self.assert_nothing_published()

    def test_target_appearing_before_commit_is_preserved(self):
        self.world()
        real_verify = cbf._verify_sources

        def verify_then_foreign(*args):
            real_verify(*args)
            self.output.mkdir()
            (self.output / "foreign.txt").write_text("other process")
        with mock.patch.object(cbf, "_verify_sources", verify_then_foreign):
            with self.assertRaisesRegex(cbf.BatchFileError, "appeared"):
                self.run_batch()
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["foreign.txt"])
        self.assert_nothing_published(keep=[self.output.name])

    def test_failure_during_publication_removes_only_own_files(self):
        self.world()
        real_replace = os.replace
        calls = []

        def failing(src, dst):
            calls.append(Path(dst).name)
            if len(calls) == 2:
                raise OSError("simulated move failure")
            return real_replace(src, dst)
        with mock.patch.object(cbf.os, "replace", failing), self.assertRaisesRegex(OSError, "simulated"):
            self.run_batch()
        self.assertEqual(calls, ["snapshot.bin", "snapshot.tiles.json"])
        self.assert_nothing_published()

    def test_complete_is_the_last_file_moved(self):
        self.world()
        real_replace = os.replace
        order = []

        def recording(src, dst):
            order.append(Path(dst).name)
            return real_replace(src, dst)
        with mock.patch.object(cbf.os, "replace", recording):
            self.run_batch()
        self.assertEqual(order[-1], "COMPLETE")
        self.assertEqual(order, ["snapshot.bin", "snapshot.tiles.json", "provenance.json", "COMPLETE"])


if __name__ == "__main__":
    unittest.main()

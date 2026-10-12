"""Tests for ore_snapshot.py with synthetic region files and UIE1 snapshots in a temporary directory.

Run from the work directory:  python -m unittest discover -s uie-map -p "test_ore_snapshot.py" -v
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

import capture_snapshot as cs  # noqa: E402
import ore_snapshot as ore  # noqa: E402
import reader  # noqa: E402
from ore_snapshot import OreSnapshotError  # noqa: E402
from test_capture_snapshot import region_bytes  # noqa: E402

GT_ID = 500
REGISTRY = {0: "minecraft:air", 1: "minecraft:stone", GT_ID: "gregtech:gt.blockores"}
AREA = cs.Area(-2, -1, 4, 3)  # chunks (-1,-1) (0,-1) (-1,0) (0,0), one per region
CHUNKS = [(-1, -1), (0, -1), (-1, 0), (0, 0)]

T_BYTE, T_SHORT, T_INT, T_STR, T_LIST, T_COMP = 1, 2, 3, 8, 9, 10


def _s(text):
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _payload(tag, value):
    formats = {T_BYTE: ">b", T_SHORT: ">h", T_INT: ">i"}
    if tag in formats:
        return struct.pack(formats[tag], value)
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


def gt(x, y, z, m=16089, n=1, m_tag=T_SHORT, tags=None):
    """GT ore tile compound; `tags` replaces or removes (value None) raw NBT tags by name."""
    record = {"id": (T_STR, "GT_TileEntity_Ores"), "x": (T_INT, x), "y": (T_INT, y), "z": (T_INT, z),
              "m": None if m is None else (m_tag, m), "n": (T_BYTE, n)}
    record.update(tags or {})
    return {k: v for k, v in record.items() if v is not None}


def chest(x, y, z):
    return {"id": (T_STR, "Chest"), "x": (T_INT, x), "y": (T_INT, y), "z": (T_INT, z),
            "Items": (T_LIST, (T_COMP, [{"Slot": (T_BYTE, 0), "id": (T_SHORT, 264)}]))}


def chunk_nbt(cx, cz, tile_entities=None):
    level = {"xPos": (T_INT, cx), "zPos": (T_INT, cz), "Sections": (T_LIST, (T_COMP, []))}
    if tile_entities is not None:
        level["TileEntities"] = tile_entities if isinstance(tile_entities, tuple) else (T_LIST, (T_COMP, tile_entities))
    return nbt({"Level": (T_COMP, level)})


def snapshot_bytes(voxels, area=AREA, registry=REGISTRY):
    body = bytearray(area.width * area.depth * cs.COLUMN_BYTES)
    for (x, y, z), block_id in voxels.items():
        pos = ((z - area.z) * area.width + (x - area.x)) * cs.COLUMN_BYTES + 1 + y * 3
        body[pos], body[pos + 1] = block_id >> 8, block_id & 0xFF
    return cs.encode_snapshot(area, registry, bytes(body))


# Base scenario: two selected ores, one GT tile outside the area, a chest with an inventory,
# a chunk with an empty list and a chunk without a TileEntities tag.
BASE_VOXELS = {(-2, 10, -1): GT_ID, (1, 20, 1): GT_ID}
BASE_TILES = {
    (-1, -1): [gt(-2, 10, -1, 16089, 1), gt(-5, 10, -1, 99999, 7, m_tag=T_INT), chest(-2, 11, -1)],
    (-1, 0): [],
    (0, 0): [gt(1, 20, 1, -5, 0)],
}


class OreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.regions = self.root / "world" / "region"
        self.regions.mkdir(parents=True)
        (self.root / "out").mkdir()
        self.snapshot = self.root / "uie-capture.bin"
        self.output = self.root / "out" / "uie-ores.json"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, tiles=None, voxels=None, chunks=CHUNKS):
        tiles = BASE_TILES if tiles is None else tiles
        self.snapshot.write_bytes(snapshot_bytes(BASE_VOXELS if voxels is None else voxels))
        by_region = {}
        for cx, cz in chunks:
            rx, rz, lx, lz = reader.chunk_to_region(cx, cz)
            by_region.setdefault((rx, rz), {})[(lx, lz)] = chunk_nbt(cx, cz, tiles.get((cx, cz)))
        for (rx, rz), records in by_region.items():
            (self.regions / f"r.{rx}.{rz}.mca").write_bytes(region_bytes(records))

    def run_ore(self, **kwargs):
        return ore.create_ore_snapshot(self.snapshot, self.regions, self.output, **kwargs)

    def assert_no_output(self):
        self.assertEqual(list((self.root / "out").iterdir()), [], "no output or temporary file may remain")

    def fails(self, pattern, tiles=None, voxels=None):
        self.write(tiles=tiles, voxels=voxels)
        with self.assertRaisesRegex(OreSnapshotError, pattern):
            self.run_ore()
        self.assert_no_output()


class SelectionTests(OreTestCase):
    def test_selects_only_gt_tiles_inside_area(self):
        self.write()
        result = self.run_ore()
        written = json.loads(self.output.read_text("utf-8"))
        self.assertEqual(written, result)
        self.assertEqual(written["format"], "UIE_ORES1")
        self.assertEqual(written["tiles"], [{"x": -2, "y": 10, "z": -1, "m": 16089, "n": 1},
                                            {"x": 1, "y": 20, "z": 1, "m": -5, "n": 0}])
        self.assertEqual(written["snapshotSha256"], hashlib.sha256(self.snapshot.read_bytes()).hexdigest())
        self.assertEqual(sorted(Path(s["path"]).name for s in written["sources"]),
                         ["r.-1.-1.mca", "r.-1.0.mca", "r.0.-1.mca", "r.0.0.mca"])
        for source in written["sources"]:
            self.assertEqual(set(source), {"path", "sha256"})
            self.assertEqual(source["sha256"], hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest())
        self.assertNotIn("Items", self.output.read_text("utf-8"))
        self.assertNotIn("Chest", self.output.read_text("utf-8"))

    def test_no_tile_entities_anywhere(self):
        self.write(tiles={}, voxels={})
        self.assertEqual(self.run_ore()["tiles"], [])

    def test_snapshot_block_mismatch(self):
        self.fails(r"'minecraft:stone' \(ID 1\)", voxels={(-2, 10, -1): GT_ID, (1, 20, 1): 1})
        self.tearDown()
        self.setUp()
        self.fails(r"'minecraft:air' \(ID 0\)", voxels={(-2, 10, -1): GT_ID})

    def test_malformed_selected_records(self):
        cases = {
            "x missing": (gt(1, 20, 1, tags={"x": None}), "x must be an integer"),
            "x string": (gt(1, 20, 1, tags={"x": (T_STR, "1")}), "x must be an integer"),
            "m too large": (gt(1, 20, 1, 40000, m_tag=T_INT), "m=40000"),
            "m too small": (gt(1, 20, 1, -40000, m_tag=T_INT), "m=-40000"),
            "m missing": (gt(1, 20, 1, m=None), "m must be an integer"),
            "n two": (gt(1, 20, 1, n=2), "n=2"),
            "n negative": (gt(1, 20, 1, n=-1), "n=-1"),
            "y too high": (gt(1, 256, 1), "y=256"),
            "y negative": (gt(1, -1, 1), "y=-1"),
            "wrong chunk": (gt(-1, 20, 1), "outside its chunk"),
            "id not string": (gt(1, 20, 1, tags={"id": (T_INT, 3)}), "id must be a string"),
        }
        for label, (record, pattern) in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.fails(pattern, tiles={(0, 0): [record]})

    def test_malformed_tile_entity_containers(self):
        self.fails("TileEntities is not a list", tiles={(0, 0): (T_INT, 5)})
        self.tearDown()
        self.setUp()
        self.fails("not a compound", tiles={(0, 0): (T_LIST, (T_INT, [1]))})

    def test_duplicate_coordinates(self):
        self.fails(r"duplicate GT ore tile at \(1,20,1\)", tiles={(0, 0): [gt(1, 20, 1), gt(1, 20, 1, 5, 0)]})

    def test_bool_coordinates_rejected_at_selection_boundary(self):
        snapshot = cs.parse_snapshot(snapshot_bytes(BASE_VOXELS))
        roots = {(0, 0): {"Level": {"TileEntities": [
            {"id": "GT_TileEntity_Ores", "x": 1, "y": 20, "z": True, "m": 1, "n": 1}]}}}
        with self.assertRaisesRegex(OreSnapshotError, "z must be an integer"):
            ore.select_ore_tiles(snapshot, roots)
        roots[(0, 0)]["Level"]["TileEntities"][0].update(z=1, n=True)
        with self.assertRaisesRegex(OreSnapshotError, "n must be an integer"):
            ore.select_ore_tiles(snapshot, roots)


class SafetyTests(OreTestCase):
    def test_missing_region_and_chunk(self):
        self.write(chunks=[(-1, -1), (0, -1), (-1, 0)])
        with self.assertRaisesRegex(OreSnapshotError, r"r\.0\.0\.mca"):
            self.run_ore()
        self.assert_no_output()
        (self.regions / "r.0.0.mca").write_bytes(region_bytes({(5, 5): chunk_nbt(5, 5)}))
        with self.assertRaisesRegex(OreSnapshotError, r"chunk \(0,0\) is not generated"):
            self.run_ore()
        self.assert_no_output()

    def test_refuses_existing_output_without_force(self):
        self.write()
        self.output.write_text("old")
        with self.assertRaisesRegex(OreSnapshotError, "--force"):
            self.run_ore()
        self.assertEqual(self.output.read_text(), "old")
        self.run_ore(force=True)
        self.assertEqual(json.loads(self.output.read_text("utf-8"))["format"], "UIE_ORES1")

    def test_refuses_input_paths_even_with_force(self):
        self.write()
        snapshot_before = self.snapshot.read_bytes()
        for target in (self.snapshot, self.regions / "r.0.0.mca", self.regions / "ores.json"):
            with self.subTest(target=target.name), self.assertRaises(OreSnapshotError):
                ore.create_ore_snapshot(self.snapshot, self.regions, target, force=True)
        self.assertEqual(self.snapshot.read_bytes(), snapshot_before)
        self.assertFalse((self.regions / "ores.json").exists())

    def test_source_change_during_read_is_detected(self):
        original = reader.RegionFile.read_chunk_nbt
        for victim_name in ("r.0.0.mca", "snapshot"):
            with self.subTest(victim=victim_name):
                self.tearDown()
                self.setUp()
                self.write()
                victim = self.snapshot if victim_name == "snapshot" else self.regions / victim_name

                def tampering(region, lx, lz, victim=victim):
                    with open(victim, "ab") as fh:
                        fh.write(b"\x00")
                    return original(region, lx, lz)

                with mock.patch.object(reader.RegionFile, "read_chunk_nbt", tampering):
                    with self.assertRaisesRegex(OreSnapshotError, "changed while reading"):
                        self.run_ore()
                self.assert_no_output()

    def test_invalid_snapshot(self):
        self.write()
        self.snapshot.write_bytes(b"UIE2" + self.snapshot.read_bytes()[4:])
        with self.assertRaises(cs.SnapshotError):
            self.run_ore()
        self.assert_no_output()


class CliTests(OreTestCase):
    def args(self, *extra):
        return ["--snapshot", str(self.snapshot), "--region-dir", str(self.regions), "--output", str(self.output),
                *extra]

    def test_exit_codes(self):
        self.write()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ore.main(self.args()), 0)
        self.assertIn("2 GT ore tiles", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ore.main(self.args()), 1)
        self.assertIn("--force", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ore.main(self.args("--force")), 0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            ore.main(["--snapshot", str(self.snapshot)])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

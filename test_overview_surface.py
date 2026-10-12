"""Tests for overview_surface.py (whole-world overview, stage A). Stdlib only; Windows and Linux."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import overview_surface as ovs
import reader
from test_prepare_warm_plan import build_world


class OverviewSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._temp.name).resolve()
        cls.level, cls.region = build_world(cls.base / "world", missing={(-5, -5)})
        cls.world = cls.base / "world"

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def test_fast_surface_equals_the_reader_surface(self):
        checked = 0
        for path in sorted(self.region.glob("r.*.mca")):
            rx, rz = reader.region_coords_from_name(path)
            with reader.RegionFile(path, rx, rz) as region:
                for loc in region.locations():
                    chunk = region.read_chunk(loc.local_x, loc.local_z)
                    expected = chunk.surface()
                    self.assertEqual(ovs.chunk_surface(chunk),
                                     (list(expected.heights), list(expected.ids), list(expected.metas)))
                    checked += 1
        self.assertGreater(checked, 50)

    def test_region_grids_place_every_column_and_mark_missing_chunks(self):
        out = self.base / "out-grid"
        out.mkdir()
        results = []
        for path in sorted(self.region.glob("r.*.mca")):
            scratch = out / ".copies"
            scratch.mkdir(exist_ok=True)
            results.append(ovs.process_region((str(path), str(out), str(scratch))))
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual(sum(r["chunks"] for r in results), 14 * 8 - 1, "one chunk is missing in the fixture")
        self.assertEqual(list((out / ".copies").iterdir()), [], "private copies removed")
        for path in sorted(self.region.glob("r.*.mca")):
            rx, rz = reader.region_coords_from_name(path)
            header, grids = ovs.read_surface(out / f"r.{rx}.{rz}.surface")
            self.assertEqual(header["region"], [rx, rz])
            self.assertEqual(header["source"]["consistency"], "observed-stable-files")
            self.assertEqual(header["source"]["size"], path.stat().st_size)
            with reader.RegionFile(path, rx, rz) as region:
                for lz in range(32):
                    for lx in range(32):
                        cx, cz = rx * 32 + lx, rz * 32 + lz
                        chunk = region.read_chunk(lx, lz)
                        for z in (0, 7, 15):
                            for x in (0, 9, 15):
                                cell = (lz * 16 + z) * 512 + lx * 16 + x
                                if chunk is None:
                                    self.assertEqual(grids["present"][cell], 0, (cx, cz))
                                    continue
                                top = chunk.surface().at(x, z)
                                self.assertEqual(grids["present"][cell], 1)
                                if top is None:
                                    self.assertEqual((grids["height"][cell], grids["ids"][2 * cell:2 * cell + 2]),
                                                     (0, b"\0\0"))
                                else:
                                    y, block, meta = top
                                    self.assertEqual(grids["height"][cell], y)
                                    self.assertEqual(int.from_bytes(grids["ids"][2 * cell:2 * cell + 2], "big"), block)
                                    self.assertEqual(grids["metas"][cell], meta)
        self.assertNotIn(((-5) % 32, (-5) % 32), set())  # missing chunk (-5,-5) is covered by present==0 above

    def test_corrupt_chunk_is_recorded_not_fatal(self):
        world = self.base / "world-corrupt"
        shutil.copytree(self.world, world)
        path = next(sorted((world / "region").glob("r.*.mca")).__iter__())
        rx, rz = reader.region_coords_from_name(path)
        with reader.RegionFile(path, rx, rz) as region:
            loc = region.locations()[0]
        raw = bytearray(path.read_bytes())
        start = loc.sector_offset * reader.SECTOR_BYTES
        raw[start + 5:start + 25] = b"\xff" * 20          # damage the compressed payload
        path.write_bytes(bytes(raw))
        out = self.base / "out-corrupt"
        result = ovs.main(["--world", str(world), "--out", str(out), "--regions", path.name, "--workers", "1"])
        run = json.loads((out / "run.json").read_text())
        self.assertEqual(result, 0)
        self.assertEqual(run["chunkErrors"], 1)
        header, grids = ovs.read_surface(out / f"r.{rx}.{rz}.surface")
        self.assertEqual(header["errors"][0]["chunk"], [rx * 32 + loc.local_x, rz * 32 + loc.local_z])
        self.assertEqual(grids["present"][(loc.local_z * 16) * 512 + loc.local_x * 16], 0)
        registry = json.loads((out / "registry.json").read_text())
        self.assertEqual(registry["format"], "UIE_SURFACE_REGISTRY1")
        self.assertTrue(registry["blocks"])

    def test_skipped_blocks_are_treated_as_air(self):
        self.assertEqual(ovs.skipped_ids({"1": "minecraft:stone", "31": "minecraft:tallgrass", "2": "Natura:Cloud",
                                          "9": "magicalcropsdeco:essence_brick", "7": "Natura:door.hopseed",
                                          "5": "PneumaticCraft:thermopneumaticProcessingPlant", "6": "minecraft:torch",
                                          "8": "InventoryPets:cloudblock", "10": "TConstruct:slime.grass",
                                          "11": "TConstruct:slime.channel", "12": "TConstruct:liquid.slime",
                                          "13": "TConstruct:CraftedSoil"}),
                         [2, 6, 8, 10, 12, 13, 31])
        path = sorted(self.region.glob("r.*.mca"))[0]
        rx, rz = reader.region_coords_from_name(path)
        with reader.RegionFile(path, rx, rz) as region:
            loc = region.locations()[0]
            chunk = region.read_chunk(loc.local_x, loc.local_z)
        heights, ids, _ = ovs.chunk_surface(chunk)
        top = ids[0]
        h2, ids2, _ = ovs.chunk_surface(chunk, frozenset({top}))
        self.assertLess(h2[0], heights[0], "the skipped top block is looked through")
        self.assertNotEqual(ids2[0], top)
        self.assertEqual(chunk.block(0, h2[0], 0)[0], ids2[0])
        for y in range(h2[0] + 1, heights[0] + 1):
            self.assertIn(chunk.block(0, y, 0)[0], (0, top))

    def test_floating_objects_fall_through_to_the_ground_but_short_gaps_stay(self):
        def section(sy, cells):
            blocks, data = bytearray(4096), bytearray(2048)
            for (x, y, z), block_id in cells.items():
                if y >> 4 == sy:
                    blocks[((y & 15) << 8) | (z << 4) | x] = block_id
            return reader.Section(sy, bytes(blocks), bytes(data))
        cells = {(x, y, z): 1 for x in range(16) for z in range(16) for y in range(64)}   # ground up to y=63
        cells[(0, 100, 0)] = 5    # floating island block: 36 air blocks below
        cells[(1, 68, 0)] = 18    # canopy: 4 air blocks below (a tree)
        cells[(2, 120, 0)] = 5    # two floating layers: both dropped
        cells[(2, 110, 0)] = 5
        chunk = reader.Chunk(0, 0, {sy: section(sy, cells) for sy in range(8)})
        heights, ids, _ = ovs.chunk_surface(chunk)
        self.assertEqual((heights[0], ids[0]), (63, 1), "floating block ignored")
        self.assertEqual((heights[1], ids[1]), (68, 18), "short gap kept")
        self.assertEqual((heights[2], ids[2]), (63, 1), "stacked floating objects ignored")
        self.assertEqual(heights[3], 63)

    def test_ground_layer_ignores_canopy_but_keeps_buildings(self):
        def section(sy, cells):
            blocks, data = bytearray(4096), bytearray(2048)
            for (x, y, z), block_id in cells.items():
                if y >> 4 == sy:
                    blocks[((y & 15) << 8) | (z << 4) | x] = block_id
            return reader.Section(sy, bytes(blocks), bytes(data))
        cells = {(x, y, z): 1 for x in range(16) for z in range(16) for y in range(64)}   # ground up to y=63
        for y in range(64, 70):
            cells[(0, y, 0)] = 17                       # trunk
        cells[(0, 70, 0)] = 18                          # leaves on top
        cells[(1, 66, 0)] = 18                          # canopy over air
        cells[(2, 64, 0)] = 4                           # a cobblestone wall: not canopy
        cells[(2, 65, 0)] = 4
        chunk = reader.Chunk(0, 0, {sy: section(sy, cells) for sy in range(5)})
        heights, _, _ = ovs.chunk_surface(chunk)
        self.assertEqual(ovs.canopy_ids({"17": "minecraft:log", "18": "minecraft:leaves", "4": "minecraft:cobblestone"}),
                         [17, 18])
        live_names = {"1": "BiomesOPlenty:bamboo", "2": "BiomesOPlenty:fruitBop", "3": "harvestcraft:beehive",
                      "5": "BiomesOPlenty:willow", "6": "BiomesOPlenty:ivy", "7": "Natura:tree", "8": "Natura:Rare Tree",
                      "9": "minecraft:snow_layer", "10": "tropicraft:tile.bambooChute",
                      "20": "minecraft:grass", "21": "minecraft:stone", "22": "minecraft:snow", "23": "Forestry:resources",
                      "24": "chisel:limestone", "25": "BiomesOPlenty:newBopGrass"}
        self.assertEqual(ovs.canopy_ids(live_names), [1, 2, 3, 5, 6, 7, 8, 9, 10])
        ground = ovs.ground_heights(chunk, heights, frozenset(), frozenset({17, 18}))
        self.assertEqual((heights[0], ground[0]), (70, 63), "tree top coloured, ground below the trunk")
        self.assertEqual((heights[1], ground[1]), (66, 63))
        self.assertEqual((heights[2], ground[2]), (65, 65), "buildings keep their height")

    def test_output_inside_the_world_is_refused_and_bad_files_rejected(self):
        with self.assertRaises(ovs.OverviewError):
            ovs.main(["--world", str(self.world), "--out", str(self.world / "x"), "--workers", "1"])
        bad = self.base / "bad.surface"
        bad.write_bytes(b"NOPE")
        with self.assertRaises(ovs.OverviewError):
            ovs.read_surface(bad)


if __name__ == "__main__":
    unittest.main()
